from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import UUID


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from gateway.factory_improvement_operator import (
    issue_captain_technical_improvements,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Issue Captain improvement authority from existing failed technical evidence."
        )
    )
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--job-id", type=UUID, action="append", required=True)
    parser.add_argument("--issued-at", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if len(args.job_id) != len(set(args.job_id)):
        raise SystemExit("improvement job IDs must be unique")
    dsn = os.environ.get("TEST_MARIADB_DSN", "").strip()
    try:
        issued_at = datetime.fromisoformat(args.issued_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("improvement issuance time is invalid") from exc
    if issued_at.tzinfo is None or issued_at.utcoffset() != timezone.utc.utcoffset(
        issued_at
    ):
        raise SystemExit("improvement issuance time must be UTC")

    issued = issue_captain_technical_improvements(
        workspace=args.workspace_root,
        authority_root=args.authority_root,
        test_mariadb_dsn=dsn,
        job_ids=tuple(args.job_id),
        clock=lambda: issued_at,
    )
    print(
        json.dumps(
            {
                "schema": "captain.factory-improvement-issuance.v1",
                "database": "captain_test",
                "status": "authorized",
                "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
                "authorizations": [
                    {
                        "job_id": str(item.request_block.job_id),
                        "failed_attempt": item.request_block.attempt,
                        "authorized_attempt": item.authorized_attempt,
                        "request_block_id": str(item.request_block.event_id),
                        "evaluation_ref": item.failed_evaluation.artifact_ref.model_dump(
                            mode="json"
                        ),
                        "authorization_ref": item.authorization_ref.model_dump(
                            mode="json"
                        ),
                    }
                    for item in issued
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
