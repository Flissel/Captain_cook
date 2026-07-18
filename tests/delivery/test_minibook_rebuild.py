from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Iterator
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector
from scripts.rebuild_minibook_projection import CaptainProjectionFeed, build_parser


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v1.json"
)
MINIBOOK_ROOT = Path(__file__).parents[2] / "minibook"


@pytest.fixture
def projection_api(tmp_path: Path) -> Iterator[MinibookClient]:
    sys.path.insert(0, str(MINIBOOK_ROOT))
    from src import main as minibook_main

    minibook_main.DB_PATH = str(tmp_path / "minibook.db")
    minibook_main.SessionLocal = None
    with TestClient(minibook_main.app) as http:
        registration = http.post(
            "/api/v1/agents",
            json={"name": f"CaptainRebuilder_{uuid4().hex}"},
        )
        assert registration.status_code == 200
        yield MinibookClient(
            "http://testserver",
            registration.json()["api_key"],
            client=http,
        )


def load_events() -> list[MinibookProjectionEvent]:
    return [
        MinibookProjectionEvent.model_validate(item)
        for item in json.loads(FIXTURE.read_text(encoding="utf-8"))
    ]


def test_rebuild_reports_and_repairs_missing_modified_duplicate_and_orphaned_posts(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    events = load_events()[:4]
    projector = MinibookProjector(
        projection_api,
        ProjectionCursorStore(tmp_path / "cursor.db"),
    )
    projector.rebuild(events[:3])
    project = projector.ensure_projection_project()
    posts = projection_api.list_posts(project["id"])
    by_event = {
        tag.removeprefix("captain-event:"): post
        for post in posts
        for tag in post["tags"]
        if tag.startswith("captain-event:")
    }
    first = by_event[str(events[0].event_id)]
    second = by_event[str(events[1].event_id)]
    projection_api.update_post(first["id"], content="operator drift")
    projection_api.create_post(
        project["id"],
        title=second["title"],
        content=second["content"],
        tags=second["tags"],
    )
    orphan = projection_api.create_post(
        project["id"],
        title="Orphaned projection",
        content="No authoritative event remains.",
        tags=["captain-projection:v1", f"captain-event:{uuid4()}"],
    )
    unrelated = projection_api.create_post(
        project["id"],
        title="Human collaboration",
        content="Must never be changed by rebuild.",
        tags=["human-owned"],
    )

    dry_run = projector.reconcile(events)

    assert dry_run.missing_event_ids == (str(events[3].event_id),)
    assert dry_run.modified_event_ids == (str(events[0].event_id),)
    assert dry_run.duplicate_event_ids == (str(events[1].event_id),)
    assert dry_run.orphaned_post_ids == (orphan["id"],)
    assert dry_run.total_changes == 4
    assert projection_api.get_post(unrelated["id"])["content"] == unrelated["content"]

    applied = projector.reconcile(events, apply=True)
    converged = projector.reconcile(events, apply=True)

    assert applied.changes_applied == 4
    assert converged.total_changes == 0
    assert converged.changes_applied == 0
    assert projection_api.get_post(unrelated["id"])["content"] == unrelated["content"]


def test_rebuild_command_is_dry_run_by_default_and_requires_apply_flag() -> None:
    required = [
        "--captain-url",
        "http://captain.test",
        "--minibook-url",
        "http://minibook.test",
        "--cursor-db",
        "cursor.db",
    ]

    assert build_parser().parse_args(required).apply is False
    assert build_parser().parse_args([*required, "--dry-run"]).apply is False
    assert build_parser().parse_args([*required, "--apply"]).apply is True


def test_captain_projection_feed_follows_public_http_pagination() -> None:
    documents = json.loads(FIXTURE.read_text(encoding="utf-8"))[:2]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"events": [documents[0]], "next_cursor": "page-2"},
                request=request,
            )
        assert cursor == "page-2"
        return httpx.Response(
            200,
            json={"events": [documents[1]], "next_cursor": None},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        feed = CaptainProjectionFeed(
            "http://captain.test",
            token="test-only",
            client=http,
        )
        events = list(feed.iter_events())

    assert [event.event_id for event in events] == [
        MinibookProjectionEvent.model_validate(item).event_id for item in documents
    ]
    assert len(requests) == 2
    assert all(
        request.url.path == "/api/v1/projections/minibook/events"
        for request in requests
    )
