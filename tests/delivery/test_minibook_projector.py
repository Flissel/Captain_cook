from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v1.json"
)
MINIBOOK_ROOT = Path(__file__).parents[2] / "minibook"


@pytest.fixture
def projection_api(tmp_path: Path) -> Iterator[tuple[TestClient, MinibookClient]]:
    sys.path.insert(0, str(MINIBOOK_ROOT))
    from src import main as minibook_main

    minibook_main.DB_PATH = str(tmp_path / "minibook.db")
    minibook_main.SessionLocal = None
    with TestClient(minibook_main.app) as http:
        registration = http.post(
            "/api/v1/agents",
            json={"name": f"CaptainProjector_{uuid4().hex}"},
        )
        assert registration.status_code == 200
        client = MinibookClient(
            "http://testserver",
            registration.json()["api_key"],
            client=http,
        )
        yield http, client


def load_events() -> list[MinibookProjectionEvent]:
    return [
        MinibookProjectionEvent.model_validate(item)
        for item in json.loads(FIXTURE.read_text(encoding="utf-8"))
    ]


def test_cursor_store_persists_feed_cursor_and_projection_identity(tmp_path: Path) -> None:
    path = tmp_path / "cursor.db"
    event = load_events()[0]
    store = ProjectionCursorStore(path)
    store.commit_event(event, post_id="post-1", content_hash="abc", feed_cursor="page-2")

    reopened = ProjectionCursorStore(path)

    assert reopened.is_processed(str(event.event_id))
    assert reopened.subject_version(event.subject_id) == 1
    assert reopened.get_feed_cursor() == "page-2"
    assert reopened.processed_count() == 1


def test_replay_is_idempotent_and_subject_versions_are_monotonic(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    events = load_events()
    store = ProjectionCursorStore(tmp_path / "cursor.db")
    projector = MinibookProjector(client, store)

    first = projector.rebuild(events)
    second = projector.rebuild(events)

    project = projector.ensure_projection_project()
    posts = client.list_posts(project["id"])
    assert [result.outcome for result in first] == ["projected"] * 8
    assert [result.outcome for result in second] == ["duplicate"] * 8
    assert len(posts) == 8
    assert store.processed_count() == 8
    assert store.subject_version("runtime-case-1") == 8
    for event in events:
        tag = f"captain-event:{event.event_id}"
        assert sum(tag in post["tags"] for post in posts) == 1


def test_out_of_order_event_is_quarantined_without_remote_overwrite(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    events = load_events()
    store = ProjectionCursorStore(tmp_path / "cursor.db")
    projector = MinibookProjector(client, store)
    projector.rebuild(events)
    project_id = projector.ensure_projection_project()["id"]
    before = client.list_posts(project_id)
    stale = events[3].model_copy(
        update={"event_id": uuid4(), "causation_id": events[-1].event_id}
    )

    result = projector.project(stale)

    assert result.outcome == "quarantined"
    assert client.list_posts(project_id) == before
    quarantine = store.list_quarantine()
    assert len(quarantine) == 1
    assert quarantine[0].event_id == str(stale.event_id)
    assert quarantine[0].reason == "stale_subject_version"


class FailOnceCursorStore(ProjectionCursorStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.failed = False

    def commit_event(
        self,
        event: MinibookProjectionEvent,
        *,
        post_id: str,
        content_hash: str,
        feed_cursor: str | None = None,
    ) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash before cursor commit")
        super().commit_event(
            event,
            post_id=post_id,
            content_hash=content_hash,
            feed_cursor=feed_cursor,
        )


def test_remote_update_before_cursor_commit_converges_on_replay(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    event = load_events()[0]
    cursor_path = tmp_path / "cursor.db"
    crashing = MinibookProjector(client, FailOnceCursorStore(cursor_path))

    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.project(event)

    project = crashing.ensure_projection_project()
    assert len(client.list_posts(project["id"])) == 1

    recovered_store = ProjectionCursorStore(cursor_path)
    recovered = MinibookProjector(client, recovered_store)
    result = recovered.project(event)

    assert result.outcome == "projected"
    assert recovered_store.is_processed(str(event.event_id))
    assert len(client.list_posts(project["id"])) == 1
