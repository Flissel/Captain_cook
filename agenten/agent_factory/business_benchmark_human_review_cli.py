"""Captain operator CLI for the durable business-benchmark review queue."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Sequence
from uuid import UUID

from agenten.agent_factory.business_benchmark_human_review import (
    CaptainHumanReviewError,
    CaptainHumanReviewStore,
    run_captain_human_review_completion_adapter,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = CaptainHumanReviewStore(Path(args.root))
        exit_code = 0
        if args.command == "list":
            reviews = store.list_reviews(status=args.status)
            payload = {
                "schema": "captain.business-benchmark-human-review-list.v1",
                "status": args.status,
                "count": len(reviews),
                "reviews": [
                    item.model_dump(mode="json", by_alias=True) for item in reviews
                ],
            }
        elif args.command == "complete":
            receipt = store.complete_review_as_operator(
                UUID(args.review_request_id),
                operator_id=args.operator_id,
                decision_code=args.decision_code,
                completed_at=datetime.fromisoformat(args.completed_at),
            )
            payload = receipt.model_dump(mode="json", by_alias=True)
        else:
            result = run_captain_human_review_completion_adapter(
                Path(args.root),
                job_ids=tuple(UUID(item) for item in args.job_id),
                operator_id=args.operator_id,
                decision_code=args.decision_code,
                expected_completions=args.expected_completions,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
            payload = result.model_dump(mode="json", by_alias=True)
            exit_code = 0 if result.status == "completed" else 2
    except (OSError, ValueError, CaptainHumanReviewError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List redacted Captain human-review requests or explicitly complete one "
            "with immutable redacted evidence."
        )
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Gitignored .captain-cook business-benchmark human-review root.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="List redacted review metadata.")
    list_command.add_argument(
        "--status",
        choices=("pending", "completed", "all"),
        default="pending",
    )
    complete = commands.add_parser(
        "complete", help="Explicitly complete one already accepted review."
    )
    complete.add_argument("--review-request-id", required=True)
    complete.add_argument("--operator-id", required=True)
    complete.add_argument("--decision-code", required=True)
    complete.add_argument(
        "--completed-at",
        required=True,
        help="Explicit timezone-aware ISO-8601 timestamp; required for exact replay.",
    )
    watch = commands.add_parser(
        "watch",
        help=(
            "Run a bounded delegated-operator adapter for exact benchmark job IDs."
        ),
    )
    watch.add_argument("--job-id", action="append", required=True)
    watch.add_argument("--operator-id", required=True)
    watch.add_argument("--decision-code", required=True)
    watch.add_argument("--expected-completions", required=True, type=int)
    watch.add_argument("--timeout-seconds", required=True, type=float)
    watch.add_argument("--poll-interval-seconds", type=float, default=0.1)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
