from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Iterator

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.delivery.minibook_client import MinibookClient  # noqa: E402
from agenten.delivery.minibook_events import MinibookProjectionEvent  # noqa: E402
from agenten.delivery.projection_cursor import ProjectionCursorStore  # noqa: E402
from agenten.delivery.projector import MinibookProjector  # noqa: E402


class CaptainProjectionFeed:
    """Paginated reader for Captain's public, redacted projection feed."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        client: httpx.Client | None = None,
        page_size: int = 100,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._page_size = page_size

    def iter_events(self, *, cursor: str | None = None) -> Iterator[MinibookProjectionEvent]:
        seen_cursors: set[str] = set()
        next_cursor = cursor
        while True:
            params: dict[str, str | int] = {"limit": self._page_size}
            if next_cursor is not None:
                params["cursor"] = next_cursor
            response = self._client.get(
                f"{self._base_url}/api/v1/projections/minibook/events",
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict) or not isinstance(document.get("events"), list):
                raise ValueError("Captain projection feed returned an invalid page")
            for raw_event in document["events"]:
                yield MinibookProjectionEvent.model_validate(raw_event)
            raw_cursor = document.get("next_cursor")
            if raw_cursor is None:
                return
            if not isinstance(raw_cursor, str) or not raw_cursor:
                raise ValueError("Captain projection feed returned an invalid cursor")
            if raw_cursor in seen_cursors:
                raise ValueError("Captain projection feed repeated a cursor")
            seen_cursors.add(raw_cursor)
            next_cursor = raw_cursor

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report or repair Minibook drift from Captain projection events"
    )
    parser.add_argument("--captain-url", required=True)
    parser.add_argument("--minibook-url", required=True)
    parser.add_argument("--cursor-db", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false")
    mode.add_argument("--apply", dest="apply", action="store_true")
    parser.set_defaults(apply=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    captain_token = os.environ.get("CAPTAIN_GATEWAY_TOKEN")
    minibook_api_key = os.environ.get("MINIBOOK_API_KEY")
    if not captain_token:
        raise SystemExit("CAPTAIN_GATEWAY_TOKEN is required")
    if not minibook_api_key:
        raise SystemExit("MINIBOOK_API_KEY is required")

    feed = CaptainProjectionFeed(args.captain_url, token=captain_token)
    minibook = MinibookClient(args.minibook_url, minibook_api_key)
    try:
        events = list(feed.iter_events())
        projector = MinibookProjector(
            minibook,
            ProjectionCursorStore(args.cursor_db),
        )
        report = projector.reconcile(events, apply=args.apply)
        output = asdict(report)
        output["total_changes"] = report.total_changes
        output["mode"] = "apply" if args.apply else "dry-run"
        print(json.dumps(output, sort_keys=True))
    finally:
        feed.close()
        minibook.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
