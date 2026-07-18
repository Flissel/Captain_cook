from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from threading import Event
from typing import Any, Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
import httpx

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v2.json"
)
MINIBOOK_ROOT = Path(__file__).parents[2] / "minibook"
PROJECTION_API_KEY = "projection-scope-test-only"


@pytest.fixture
def projection_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, MinibookClient]]:
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", PROJECTION_API_KEY)
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
            "http://127.0.0.1",
            registration.json()["api_key"],
            projection_api_key=PROJECTION_API_KEY,
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
    assert store.subject_version(events[0].subject_id) == 8
    for event in events:
        assert sum(
            f"captain-event:{event.event_id}" in post["tags"] for post in posts
        ) == 1


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


def test_unsafe_model_copy_is_revalidated_before_any_minibook_write(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    event = load_events()[0]
    unsafe = event.model_copy(
        update={
            "payload": event.payload.model_copy(
                update={
                    "evidence_summary": "Authorization: Bearer fake-review-token-123456"
                }
            )
        }
    )
    projector = MinibookProjector(
        client,
        ProjectionCursorStore(tmp_path / "cursor.db"),
    )

    with pytest.raises(ValueError):
        projector.project(unsafe)

    assert client.list_projects() == []


class BlockingSearchClient:
    def __init__(self, delegate: MinibookClient) -> None:
        self.delegate = delegate
        self.search_completed = Event()
        self.release_search = Event()

    def upsert_projection_post(
        self, *args: object, **kwargs: object
    ) -> dict[str, Any]:
        self.search_completed.set()
        assert self.release_search.wait(timeout=5)
        return self.delegate.upsert_projection_post(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def test_two_concurrent_projectors_create_exactly_one_post_for_same_event(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    http, client = projection_api
    event = load_events()[0]
    cursor_path = tmp_path / "cursor.db"
    first_client = BlockingSearchClient(client)
    second_client = MinibookClient(
        "http://127.0.0.1",
        client._headers["Authorization"].removeprefix("Bearer "),
        projection_api_key=PROJECTION_API_KEY,
        client=http,
    )
    first = MinibookProjector(first_client, ProjectionCursorStore(cursor_path))
    second = MinibookProjector(second_client, ProjectionCursorStore(cursor_path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.project, event)
        assert first_client.search_completed.wait(timeout=5)
        second_future = pool.submit(second.project, event)
        second_result = second_future.result(timeout=5)
        first_client.release_search.set()
        first_result = first_future.result(timeout=5)

    project = first.ensure_projection_project()
    assert {first_result.outcome, second_result.outcome} == {"projected", "busy"}
    assert len(client.list_posts(project["id"])) == 1


def test_lower_version_cannot_write_after_newer_subject_claim(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    http, client = projection_api
    lower, newer = load_events()[:2]
    cursor_path = tmp_path / "cursor.db"
    newer_client = BlockingSearchClient(client)
    lower_client = MinibookClient(
        "http://127.0.0.1",
        client._headers["Authorization"].removeprefix("Bearer "),
        projection_api_key=PROJECTION_API_KEY,
        client=http,
    )
    newer_projector = MinibookProjector(
        newer_client,
        ProjectionCursorStore(cursor_path),
    )
    lower_projector = MinibookProjector(
        lower_client,
        ProjectionCursorStore(cursor_path),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        newer_future = pool.submit(newer_projector.project, newer)
        assert newer_client.search_completed.wait(timeout=5)
        lower_result = pool.submit(lower_projector.project, lower).result(timeout=5)
        newer_client.release_search.set()
        newer_result = newer_future.result(timeout=5)

    project = newer_projector.ensure_projection_project()
    assert newer_result.outcome == "projected"
    assert lower_result.outcome == "quarantined"
    assert len(client.list_posts(project["id"])) == 1
    assert ProjectionCursorStore(cursor_path).list_quarantine()[0].reason == (
        "stale_subject_version"
    )


class FailCompleteClaimStore(ProjectionCursorStore):
    def complete_claim(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated crash before cursor commit")


def test_remote_write_recovers_after_claim_expiry_without_duplicate_post(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    event = load_events()[0]
    cursor_path = tmp_path / "cursor.db"
    now = [datetime(2026, 7, 18, tzinfo=timezone.utc)]
    def clock() -> datetime:
        return now[0]
    crashing = MinibookProjector(
        client,
        FailCompleteClaimStore(cursor_path, clock=clock),
        claim_ttl=timedelta(seconds=5),
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.project(event)

    project = crashing.ensure_projection_project()
    assert len(client.list_posts(project["id"])) == 1
    recovering = MinibookProjector(
        client,
        ProjectionCursorStore(cursor_path, clock=clock),
        claim_ttl=timedelta(seconds=5),
    )
    assert recovering.project(event).outcome == "busy"

    now[0] += timedelta(seconds=6)
    assert recovering.project(event).outcome == "projected"
    assert len(client.list_posts(project["id"])) == 1
    assert ProjectionCursorStore(cursor_path).is_processed(str(event.event_id))


def test_legacy_empty_event_fingerprint_fails_closed_and_quarantines(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    _, client = projection_api
    event = load_events()[0]
    cursor_path = tmp_path / "cursor.db"
    store = ProjectionCursorStore(cursor_path)
    store.commit_event(event, post_id="legacy-post", content_hash="legacy")
    with sqlite3.connect(cursor_path) as connection:
        connection.execute(
            "UPDATE processed_projection_events SET event_fingerprint = '' "
            "WHERE event_id = ?",
            (str(event.event_id),),
        )

    result = MinibookProjector(client, store).project(event)

    assert result.outcome == "quarantined"
    assert client.list_projects() == []
    quarantine = store.list_quarantine()
    assert quarantine[0].reason == "unverifiable_legacy_event_fingerprint"
    assert quarantine[0].retryable is False


class BlockingProjectionUpsertClient:
    def __init__(self, delegate: MinibookClient) -> None:
        self.delegate = delegate
        self.upsert_started = Event()
        self.release_upsert = Event()

    def upsert_projection_post(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.upsert_started.set()
        assert self.release_upsert.wait(timeout=5)
        return self.delegate.upsert_projection_post(*args, **kwargs)

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)


def test_expired_lower_writer_cannot_overwrite_newer_remote_subject_version(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    http, client = projection_api
    lower, newer = load_events()[:2]
    cursor_path = tmp_path / "cursor.db"
    now = [datetime(2026, 7, 18, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return now[0]

    blocked_client = BlockingProjectionUpsertClient(client)
    writer_a = MinibookProjector(
        blocked_client,
        ProjectionCursorStore(cursor_path, clock=clock),
        claim_ttl=timedelta(seconds=5),
    )
    writer_b = MinibookProjector(
        MinibookClient(
            "http://127.0.0.1",
            client._headers["Authorization"].removeprefix("Bearer "),
            projection_api_key=PROJECTION_API_KEY,
            client=http,
        ),
        ProjectionCursorStore(cursor_path, clock=clock),
        claim_ttl=timedelta(seconds=5),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_future = pool.submit(writer_a.project, lower)
        assert blocked_client.upsert_started.wait(timeout=5)
        now[0] += timedelta(seconds=6)
        newer_result = pool.submit(writer_b.project, newer).result(timeout=5)
        blocked_client.release_upsert.set()
        stale_result = stale_future.result(timeout=5)

    project = writer_b.ensure_projection_project()
    posts = client.list_posts(project["id"])
    assert newer_result.outcome == "projected"
    assert stale_result.outcome == "quarantined"
    assert len(posts) == 1
    assert f"captain-event:{newer.event_id}" in posts[0]["tags"]
    assert f"captain-version:{newer.subject_version}" in posts[0]["tags"]


def test_different_subject_projectors_create_one_projection_project_concurrently(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
) -> None:
    http, client = projection_api
    first_event, second_event = load_events()[:2]
    second_event = second_event.model_copy(
        update={"subject_id": f"subject:{uuid4()}", "subject_version": 1}
    )
    other = MinibookClient(
        "http://127.0.0.1",
        client._headers["Authorization"].removeprefix("Bearer "),
        projection_api_key=PROJECTION_API_KEY,
        client=http,
    )
    first_projector = MinibookProjector(
        client,
        ProjectionCursorStore(tmp_path / "first-cursor.db"),
    )
    second_projector = MinibookProjector(
        other,
        ProjectionCursorStore(tmp_path / "second-cursor.db"),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_projector.project, first_event)
        second = pool.submit(second_projector.project, second_event)
        outcomes = {first.result(timeout=5).outcome, second.result(timeout=5).outcome}

    assert outcomes == {"projected"}
    assert len(client.list_projects()) == 1
    assert len(client.list_posts("captain-runtime-projection-v2")) == 2


def test_ordinary_registered_agent_cannot_call_projection_put_routes(
    projection_api: tuple[TestClient, MinibookClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, client = projection_api
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", "projection-scope-test-only")
    ordinary_headers = client._headers
    ordinary_project = http.post(
        "/api/v1/projects",
        headers=ordinary_headers,
        json={"name": "Ordinary member project", "description": "membership proof"},
    )
    ordinary_project.raise_for_status()

    project_response = http.put(
        "/api/v1/projection-projects/captain-runtime-projection-v2",
        headers=ordinary_headers,
        json={},
    )
    post_response = http.put(
        f"/api/v1/projects/{ordinary_project.json()['id']}/projection-post",
        headers=ordinary_headers,
        json=load_events()[0].model_dump(mode="json", by_alias=True),
    )

    assert project_response.status_code == 403
    assert post_response.status_code == 403
    assert "projection-scope-test-only" not in (
        project_response.text + post_response.text
    )


def test_dedicated_projection_credential_renders_canonical_event_server_side(
    projection_api: tuple[TestClient, MinibookClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _ = projection_api
    projection_token = "projection-scope-test-only"
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", projection_token)
    headers = {"Authorization": f"Bearer {projection_token}"}
    event_document = load_events()[0].model_dump(mode="json", by_alias=True)

    project = http.put(
        "/api/v1/projection-projects/captain-runtime-projection-v2",
        headers=headers,
        json={},
    )
    post = http.put(
        "/api/v1/projects/captain-runtime-projection-v2/projection-post",
        headers=headers,
        json=event_document,
    )
    tampered = http.put(
        "/api/v1/projects/captain-runtime-projection-v2/projection-post",
        headers=headers,
        json={**event_document, "title": "sk-proj-must-never-render"},
    )

    assert project.status_code == 200
    assert post.status_code == 200
    assert post.json()["title"] == "[plan.requested] Runtime planning requested"
    assert "Requested" in post.json()["content"]
    assert "captain-projection:v2" in post.json()["tags"]
    assert tampered.status_code == 422
    assert projection_token not in project.text + post.text + tampered.text


def test_projection_client_sends_only_structured_v2_event_with_scoped_bearer() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "projection-post"}, request=request)

    event = load_events()[0]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = MinibookClient(
            "https://minibook.example",
            "ordinary-agent-key",
            projection_api_key="projection-scope-key",
            client=http,
        )
        client.upsert_projection_post(
            "captain-runtime-projection-v2",
            event=event,
        )

    body = json.loads(requests[0].content)
    assert body == event.model_dump(mode="json", by_alias=True)
    assert not {"title", "content", "tags", "source_fingerprint"} & body.keys()
    assert requests[0].headers["Authorization"] == "Bearer projection-scope-key"


def test_remote_rejects_conflicting_reuse_of_same_event_id(
    tmp_path: Path,
    projection_api: tuple[TestClient, MinibookClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, client = projection_api
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", "projection-scope-test-only")
    original = load_events()[0]
    conflicting = original.model_copy(
        update={
            "payload": original.payload.model_copy(
                update={"batch_version": original.payload.batch_version + 1}
            )
        }
    )
    scoped_client = MinibookClient(
        "http://127.0.0.1",
        client._headers["Authorization"].removeprefix("Bearer "),
        projection_api_key="projection-scope-test-only",
        client=http,
    )
    first = MinibookProjector(
        scoped_client,
        ProjectionCursorStore(tmp_path / "first.db"),
    )
    conflicting_writer = MinibookProjector(
        scoped_client,
        ProjectionCursorStore(tmp_path / "second.db"),
    )

    assert first.project(original).outcome == "projected"
    result = conflicting_writer.project(conflicting)

    assert result.outcome == "quarantined"
    assert conflicting_writer.cursor_store is not None
    assert conflicting_writer.cursor_store.list_quarantine()[0].reason == (
        "remote_projection_conflict"
    )
    posts = scoped_client.list_posts("captain-runtime-projection-v2")
    assert len(posts) == 1
    assert f"captain-event:{original.event_id}" in posts[0]["tags"]
