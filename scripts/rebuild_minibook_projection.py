from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Iterator

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.delivery.minibook_client import (  # noqa: E402
    MinibookClient,
    validate_service_base_url,
)
from agenten.delivery.minibook_events import MinibookProjectionEvent  # noqa: E402
from agenten.delivery.projection_cursor import ProjectionCursorStore  # noqa: E402
from agenten.delivery.projector import (  # noqa: E402
    ConflictingProjectionEvent,
    MinibookProjector,
    ProjectionResult,
)


@dataclass(frozen=True)
class ProjectionFeedPage:
    events: tuple[MinibookProjectionEvent, ...]
    cursor: str
    has_more: bool


class ProjectionPageIncomplete(RuntimeError):
    """Raised when a page cannot reach a terminal projected/quarantined state."""


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
        self._base_url = validate_service_base_url(base_url)
        self._token = token
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._page_size = page_size
        self.last_cursor: str | None = None

    def iter_pages(self, *, cursor: str | None = None) -> Iterator[ProjectionFeedPage]:
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
            raw_cursor = document.get("cursor")
            if not isinstance(raw_cursor, str) or not raw_cursor:
                raise ValueError("Captain projection feed returned an invalid cursor")
            has_more = document.get("has_more")
            if not isinstance(has_more, bool):
                raise ValueError("Captain projection feed returned invalid pagination state")
            if has_more and (raw_cursor == next_cursor or raw_cursor in seen_cursors):
                raise ValueError("Captain projection feed repeated a cursor")
            events = tuple(
                MinibookProjectionEvent.model_validate(raw_event)
                for raw_event in document["events"]
            )
            yield ProjectionFeedPage(
                events=events,
                cursor=raw_cursor,
                has_more=has_more,
            )
            self.last_cursor = raw_cursor
            if not has_more:
                return
            seen_cursors.add(raw_cursor)
            next_cursor = raw_cursor

    def iter_events(self, *, cursor: str | None = None) -> Iterator[MinibookProjectionEvent]:
        for page in self.iter_pages(cursor=cursor):
            yield from page.events

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def consume_incremental_projection(
    feed: CaptainProjectionFeed,
    projector: MinibookProjector,
    cursor_store: ProjectionCursorStore,
    *,
    apply: bool,
) -> list[ProjectionResult]:
    """Consume complete feed pages and checkpoint only terminal page outcomes."""
    results: list[ProjectionResult] = []
    if apply:
        cursor_store.validate_incremental_v2_state()
    start_cursor = cursor_store.get_feed_cursor()
    for page in feed.iter_pages(cursor=start_cursor):
        deduplicated = projector.deduplicate_events(
            page.events,
            quarantine_conflicts=apply,
        )
        if not apply:
            if deduplicated.conflicting_event_ids:
                raise ConflictingProjectionEvent(deduplicated.conflicting_event_ids)
            continue
        page_results = list(deduplicated.quarantined)
        page_results.extend(projector.project(event) for event in deduplicated.events)
        if any(result.outcome == "busy" for result in page_results):
            raise ProjectionPageIncomplete(
                f"projection feed page {page.cursor!r} still has claimed events"
            )
        cursor_store.checkpoint_v2_feed(page.cursor)
        results.extend(page_results)
    return results


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
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="read the authoritative feed from the beginning instead of resuming",
    )
    parser.set_defaults(apply=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    captain_token = os.environ.get("CAPTAIN_GATEWAY_TOKEN")
    minibook_api_key = os.environ.get("MINIBOOK_API_KEY")
    projection_api_key = os.environ.get("MINIBOOK_PROJECTION_API_KEY")
    if not captain_token:
        raise SystemExit("CAPTAIN_GATEWAY_TOKEN is required")
    if not minibook_api_key:
        raise SystemExit("MINIBOOK_API_KEY is required")
    if args.apply and not projection_api_key:
        raise SystemExit("MINIBOOK_PROJECTION_API_KEY is required for --apply")

    feed = CaptainProjectionFeed(args.captain_url, token=captain_token)
    minibook = MinibookClient(
        args.minibook_url,
        minibook_api_key,
        projection_api_key=projection_api_key,
    )
    try:
        if args.apply and not args.full_rebuild:
            cursor_store = ProjectionCursorStore(args.cursor_db)
            projector = MinibookProjector(minibook, cursor_store)
            results = consume_incremental_projection(
                feed,
                projector,
                cursor_store,
                apply=True,
            )
            output = {
                "mode": "incremental-apply",
                "processed": len(results),
                "outcomes": [result.outcome for result in results],
                "cursor": cursor_store.get_feed_cursor(),
            }
        else:
            projector = MinibookProjector(minibook)
            events = list(feed.iter_events(cursor=None))
            if args.apply:
                if feed.last_cursor is None:
                    raise RuntimeError("full projection feed has no terminal cursor")
                cursor_store = ProjectionCursorStore(args.cursor_db)
                cursor_store.begin_v2_full_rebuild()
                projector = MinibookProjector(minibook, cursor_store)
                rebuild_results = projector.rebuild(events)
                incomplete = [
                    result
                    for result in rebuild_results
                    if result.outcome not in {"projected", "duplicate"}
                ]
                if incomplete:
                    raise ProjectionPageIncomplete(
                        "full rebuild has non-projectable authoritative events"
                    )
                report = projector.reconcile(events, apply=True)
                cursor_store.checkpoint_v2_feed(feed.last_cursor)
            else:
                report = projector.reconcile(events, apply=False)
            output = asdict(report)
            output["total_changes"] = report.total_changes
            output["mode"] = "full-rebuild" if args.apply else "dry-run"
        print(json.dumps(output, sort_keys=True))
    finally:
        feed.close()
        minibook.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
