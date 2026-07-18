from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterator
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
import scripts.rebuild_minibook_projection as rebuild_script

from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.minibook_events import MinibookProjectionEvent
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projector import MinibookProjector
from agenten.delivery.projector import ConflictingProjectionEvent
from scripts.rebuild_minibook_projection import (
    CaptainProjectionFeed,
    build_parser,
    consume_incremental_projection,
)


FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "minibook_projection.v2.json"
)
MINIBOOK_ROOT = Path(__file__).parents[2] / "minibook"
PROJECTION_API_KEY = "projection-scope-test-only"


def _legacy_v1_tags(
    *,
    title: str,
    content: str,
    event_id: str,
    correlation_id: str,
    subject_id: str,
    subject_version: int,
    view: str,
) -> list[str]:
    identity_tags = (
        "captain-projection:v1",
        f"captain-event:{event_id}",
        f"captain-correlation:{correlation_id}",
        f"captain-subject:{subject_id}",
        f"captain-version:{subject_version}",
        f"captain-view:{view}",
    )
    canonical = json.dumps(
        {"title": title, "content": content, "tags": identity_tags},
        sort_keys=True,
        separators=(",", ":"),
    )
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return [*identity_tags, f"captain-hash:{content_hash}"]


@pytest.fixture
def projection_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[MinibookClient]:
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", PROJECTION_API_KEY)
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
            "http://127.0.0.1",
            registration.json()["api_key"],
            projection_api_key=PROJECTION_API_KEY,
            client=http,
        )


def load_events() -> list[MinibookProjectionEvent]:
    return [
        MinibookProjectionEvent.model_validate(item)
        for item in json.loads(FIXTURE.read_text(encoding="utf-8"))
    ]


def _seed_projection_post(
    client: MinibookClient,
    *,
    project_id: str,
    title: str,
    content: str,
    tags: list[str],
) -> dict[str, Any]:
    """Inject pre-boundary drift directly; ordinary HTTP must stay fenced."""

    from src import main as minibook_main
    from src.models import Post

    with minibook_main.SessionLocal() as db:
        post = Post(
            project_id=project_id,
            author_id="captain-projection-service-v2",
            title=title,
            content=content,
            type="plan",
        )
        post.tags = tags
        db.add(post)
        db.commit()
        post_id = str(post.id)
    return client.get_post(post_id)


def _seed_real_v1_project() -> dict[str, str]:
    from src import main as minibook_main
    from src.models import Agent, Comment, GitHubWebhook, Post, Project, ProjectMember, Webhook

    with minibook_main.SessionLocal() as db:
        author = Agent(name=f"LegacyRebuilder_{uuid4().hex}")
        db.add(author)
        db.flush()
        legacy = Project(
            id=f"legacy-{uuid4().hex}",
            name="Captain Runtime Projection",
            description="Historical v1 projection",
            primary_lead_agent_id=author.id,
        )
        db.add(legacy)
        db.flush()
        membership = ProjectMember(
            agent_id=author.id,
            project_id=legacy.id,
            role="legacy-lead",
        )
        event_id = str(uuid4())
        correlation_id = str(uuid4())
        subject_id = f"legacy-subject-{uuid4().hex}"
        marked = Post(
            project_id=legacy.id,
            author_id=author.id,
            title="Legacy v1 projection",
            content="Legacy public content",
            type="plan",
        )
        marked.tags = _legacy_v1_tags(
            title=marked.title,
            content=marked.content,
            event_id=event_id,
            correlation_id=correlation_id,
            subject_id=subject_id,
            subject_version=1,
            view="plan",
        )
        human = Post(
            project_id=legacy.id,
            author_id=author.id,
            title="Human note",
            content="Preserve this unrelated note",
        )
        human.tags = ["human"]
        db.add_all((membership, marked, human))
        db.flush()
        comment = Comment(
            post_id=marked.id,
            author_id=author.id,
            content="Legacy projection comment",
        )
        webhook = Webhook(project_id=legacy.id, url="https://example.test/legacy")
        github = GitHubWebhook(project_id=legacy.id, secret="legacy-fixture-only")
        db.add_all((comment, webhook, github))
        db.commit()
        return {
            "legacy_id": str(legacy.id),
            "marked_id": str(marked.id),
            "human_id": str(human.id),
            "membership_id": str(membership.id),
            "webhook_id": str(webhook.id),
            "github_id": str(github.id),
            "comment_id": str(comment.id),
            "event_id": event_id,
            "subject_id": subject_id,
        }


def _corrupt_projection_post(post_id: str, *, content: str) -> None:
    from src import main as minibook_main
    from src.models import Post

    with minibook_main.SessionLocal() as db:
        post = db.query(Post).filter(Post.id == post_id).one()
        post.content = content
        db.commit()


def test_rebuild_reports_and_repairs_missing_modified_duplicate_and_orphaned_posts(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    events = [
        event.model_copy(
            update={
                "subject_id": f"subject:{uuid4()}",
                "subject_version": 1,
            }
        )
        for event in load_events()[:4]
    ]
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
    _corrupt_projection_post(first["id"], content="operator drift")
    _seed_projection_post(
        projection_api,
        project_id=project["id"],
        title=second["title"],
        content=second["content"],
        tags=second["tags"],
    )
    orphan = _seed_projection_post(
        projection_api,
        project_id=project["id"],
        title="Orphaned projection",
        content="No authoritative event remains.",
        tags=["captain-projection:v2", f"captain-event:{uuid4()}"],
    )
    unrelated = _seed_projection_post(
        projection_api,
        project_id=project["id"],
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
    assert build_parser().parse_args([*required, "--apply"]).full_rebuild is False
    assert build_parser().parse_args([*required, "--apply", "--full-rebuild"]).full_rebuild


def test_captain_projection_feed_follows_public_http_pagination() -> None:
    documents = json.loads(FIXTURE.read_text(encoding="utf-8"))[:2]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"events": [documents[0]], "cursor": "page-2", "has_more": True},
                request=request,
            )
        assert cursor == "page-2"
        return httpx.Response(
            200,
            json={"events": [documents[1]], "cursor": "page-end", "has_more": False},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        feed = CaptainProjectionFeed(
            "https://captain.test",
            token="test-only",
            client=http,
        )
        pages = list(feed.iter_pages())

    assert [event.event_id for page in pages for event in page.events] == [
        MinibookProjectionEvent.model_validate(item).event_id for item in documents
    ]
    assert [page.cursor for page in pages] == ["page-2", "page-end"]
    assert len(requests) == 2
    assert all(
        request.url.path == "/api/v1/projections/minibook/events"
        for request in requests
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://captain.example",
        "file:///private/feed.json",
        "http://captain.example",
        "https://user:pass@captain.example",
    ],
)
def test_captain_feed_rejects_unsupported_or_insecure_token_targets(
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        CaptainProjectionFeed(base_url, token="never-send")


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://minibook.example",
        "file:///private/minibook.db",
        "http://minibook.example",
        "https://user:pass@minibook.example",
    ],
)
def test_minibook_client_rejects_unsupported_or_insecure_token_targets(
    base_url: str,
) -> None:
    with pytest.raises(ValueError):
        MinibookClient(base_url, "never-send")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:3456",
        "http://localhost:3456",
        "http://[::1]:3456",
        "https://captain.example",
    ],
)
def test_http_loopback_and_https_service_urls_are_allowed(base_url: str) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as http:
        feed = CaptainProjectionFeed(base_url, token="test-only", client=http)
        minibook = MinibookClient(base_url, "test-only", client=http)

    feed.close()
    minibook.close()


def test_injected_minibook_transport_cannot_redirect_bearer_to_its_own_base_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[], request=request)

    with httpx.Client(
        base_url="http://insecure.example",
        transport=httpx.MockTransport(handler),
    ) as http:
        client = MinibookClient("https://minibook.example", "test-only", client=http)
        client.list_projects()

    assert requests[0].url == "https://minibook.example/api/v1/projects"


def test_reconcile_deduplicates_identical_authoritative_events(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    event = load_events()[0]
    projector = MinibookProjector(
        projection_api,
        ProjectionCursorStore(tmp_path / "cursor.db"),
    )

    first = projector.reconcile([event, event], apply=True)
    second = projector.reconcile([event, event], apply=True)

    project = projector.ensure_projection_project()
    assert first.changes_applied == 1
    assert second.total_changes == 0
    assert len(projection_api.list_posts(project["id"])) == 1


def test_reconcile_quarantines_conflicting_duplicate_event_ids_before_write(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    event = load_events()[0]
    conflicting = event.model_copy(
        update={
            "payload": event.payload.model_copy(
                update={"batch_version": event.payload.batch_version + 1}
            )
        }
    )
    store = ProjectionCursorStore(tmp_path / "cursor.db")
    projector = MinibookProjector(projection_api, store)

    with pytest.raises(ConflictingProjectionEvent):
        projector.reconcile([event, conflicting], apply=True)

    assert projection_api.list_projects() == []
    assert store.list_quarantine()[0].reason == "conflicting_duplicate_event_id"


class FailCursorCommitStore(ProjectionCursorStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.failed = False

    def checkpoint_v2_feed(self, cursor: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash before page cursor commit")
        super().checkpoint_v2_feed(cursor)


def _single_page_feed(
    documents: list[dict[str, object]],
    *,
    cursor: str,
    requests: list[httpx.Request] | None = None,
) -> CaptainProjectionFeed:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            200,
            json={"events": documents, "cursor": cursor, "has_more": False},
            request=request,
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaptainProjectionFeed("https://captain.test", token="test-only", client=http)


def test_incremental_restart_resumes_from_committed_page_cursor(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    documents = json.loads(FIXTURE.read_text(encoding="utf-8"))[:2]
    cursor_path = tmp_path / "cursor.db"
    store = ProjectionCursorStore(cursor_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(
                200,
                json={"events": [documents[0]], "cursor": "after-1", "has_more": True},
                request=request,
            )
        if cursor == "after-1":
            return httpx.Response(
                200,
                json={"events": [documents[1]], "cursor": "after-2", "has_more": False},
                request=request,
            )
        assert cursor == "after-2"
        return httpx.Response(
            200,
            json={"events": [], "cursor": "after-2", "has_more": False},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        feed = CaptainProjectionFeed(
            "https://captain.test", token="test-only", client=http
        )
        projector = MinibookProjector(projection_api, store)
        consume_incremental_projection(feed, projector, store, apply=True)
        consume_incremental_projection(feed, projector, store, apply=True)

    assert store.get_feed_cursor() == "after-2"
    assert [request.url.params.get("cursor") for request in requests] == [
        None,
        "after-1",
        "after-2",
    ]


def test_page_replay_after_cursor_commit_crash_converges_without_duplicate(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    cursor_path = tmp_path / "cursor.db"
    crashing_store = FailCursorCommitStore(cursor_path)
    projector = MinibookProjector(projection_api, crashing_store)

    with pytest.raises(RuntimeError, match="page cursor commit"):
        consume_incremental_projection(
            _single_page_feed([document], cursor="after-1"),
            projector,
            crashing_store,
            apply=True,
        )

    assert ProjectionCursorStore(cursor_path).get_feed_cursor() is None
    recovered_store = ProjectionCursorStore(cursor_path)
    results = consume_incremental_projection(
        _single_page_feed([document], cursor="after-1"),
        MinibookProjector(projection_api, recovered_store),
        recovered_store,
        apply=True,
    )
    project = projector.ensure_projection_project()
    assert [result.outcome for result in results] == ["duplicate"]
    assert recovered_store.get_feed_cursor() == "after-1"
    assert len(projection_api.list_posts(project["id"])) == 1


def test_stale_page_event_advances_cursor_after_quarantine(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    lower, newer = load_events()[:2]
    store = ProjectionCursorStore(tmp_path / "cursor.db")
    projector = MinibookProjector(projection_api, store)
    assert projector.project(newer).outcome == "projected"

    results = consume_incremental_projection(
        _single_page_feed(
            [lower.model_dump(mode="json", by_alias=True)],
            cursor="after-stale",
        ),
        projector,
        store,
        apply=True,
    )

    assert [result.outcome for result in results] == ["quarantined"]
    assert store.get_feed_cursor() == "after-stale"
    assert store.list_quarantine()[0].reason == "stale_subject_version"


def test_conflicting_page_events_advance_cursor_after_quarantine(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    lower, newer = load_events()[:2]
    conflict = lower.model_copy(
        update={
            "payload": lower.payload.model_copy(
                update={"batch_version": lower.payload.batch_version + 1}
            )
        }
    )
    store = ProjectionCursorStore(tmp_path / "cursor.db")
    projector = MinibookProjector(projection_api, store)
    assert projector.project(newer).outcome == "projected"
    documents = [
        item.model_dump(mode="json", by_alias=True)
        for item in (lower, conflict)
    ]

    results = consume_incremental_projection(
        _single_page_feed(documents, cursor="after-quarantine"),
        projector,
        store,
        apply=True,
    )

    assert [result.outcome for result in results] == ["quarantined"]
    assert store.get_feed_cursor() == "after-quarantine"
    assert store.list_quarantine()[0].reason == "conflicting_duplicate_event_id"


def test_incremental_dry_run_does_not_write_or_advance_cursor(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    store = ProjectionCursorStore(tmp_path / "cursor.db")

    results = consume_incremental_projection(
        _single_page_feed([document], cursor="after-dry-run"),
        MinibookProjector(projection_api, store),
        store,
        apply=False,
    )

    assert results == []
    assert store.get_feed_cursor() is None
    assert projection_api.list_projects() == []


def test_v1_feed_document_does_not_checkpoint_or_write(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    document["schema"] = "captain.minibook-projection.v1"
    store = ProjectionCursorStore(tmp_path / "cursor.db")

    with pytest.raises(ValueError):
        consume_incremental_projection(
            _single_page_feed([document], cursor="must-not-commit"),
            MinibookProjector(projection_api, store),
            store,
            apply=True,
        )

    assert store.get_feed_cursor() is None
    assert store.get_contract_version() is None
    assert projection_api.list_projects() == []


def test_default_cli_dry_run_reports_missing_without_creating_project(
    tmp_path: Path,
    projection_api: MinibookClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    feed = _single_page_feed([document], cursor="after-dry-run")
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "test-only")
    monkeypatch.setenv("MINIBOOK_API_KEY", "test-only")
    monkeypatch.setattr(rebuild_script, "CaptainProjectionFeed", lambda *a, **k: feed)
    monkeypatch.setattr(rebuild_script, "MinibookClient", lambda *a, **k: projection_api)

    cursor_path = tmp_path / "absent" / "nested" / "cursor.db"
    assert not cursor_path.parent.exists()

    exit_code = rebuild_script.main(
        [
            "--captain-url",
            "https://captain.test",
            "--minibook-url",
            "http://127.0.0.1",
            "--cursor-db",
            str(cursor_path),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["mode"] == "dry-run"
    assert output["missing_event_ids"] == [document["event_id"]]
    assert projection_api.list_projects() == []
    assert not cursor_path.parent.exists()


def test_successful_apply_full_rebuild_checkpoints_terminal_feed_cursor(
    tmp_path: Path,
    projection_api: MinibookClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    feed = _single_page_feed([document], cursor="after-full-rebuild")
    cursor_path = tmp_path / "cursor.db"
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "test-only")
    monkeypatch.setenv("MINIBOOK_API_KEY", "test-only")
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", PROJECTION_API_KEY)
    monkeypatch.setattr(rebuild_script, "CaptainProjectionFeed", lambda *a, **k: feed)
    monkeypatch.setattr(rebuild_script, "MinibookClient", lambda *a, **k: projection_api)

    exit_code = rebuild_script.main(
        [
            "--captain-url",
            "https://captain.test",
            "--minibook-url",
            "http://127.0.0.1",
            "--cursor-db",
            str(cursor_path),
            "--apply",
            "--full-rebuild",
        ]
    )

    assert exit_code == 0
    assert ProjectionCursorStore(cursor_path).get_feed_cursor() == "after-full-rebuild"


def test_incremental_requires_explicit_full_rebuild_for_unversioned_cursor(
    tmp_path: Path,
    projection_api: MinibookClient,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    store = ProjectionCursorStore(tmp_path / "legacy-cursor.db")
    store.set_feed_cursor("legacy-v1-position")

    with pytest.raises(RuntimeError, match="full rebuild"):
        consume_incremental_projection(
            _single_page_feed([document], cursor="after-v2"),
            MinibookProjector(projection_api, store),
            store,
            apply=True,
        )

    assert store.get_feed_cursor() == "legacy-v1-position"
    assert projection_api.list_projects() == []


def test_apply_full_rebuild_retires_v1_posts_and_records_v2_state(
    tmp_path: Path,
    projection_api: MinibookClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    legacy = _seed_real_v1_project()
    feed = _single_page_feed([document], cursor="after-v2-cutover")
    cursor_path = tmp_path / "cursor.db"
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "test-only")
    monkeypatch.setenv("MINIBOOK_API_KEY", "test-only")
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", "projection-scope-test-only")
    monkeypatch.setattr(rebuild_script, "CaptainProjectionFeed", lambda *a, **k: feed)
    monkeypatch.setattr(rebuild_script, "MinibookClient", lambda *a, **k: projection_api)

    assert rebuild_script.main(
        [
            "--captain-url",
            "https://captain.test",
            "--minibook-url",
            "http://127.0.0.1",
            "--cursor-db",
            str(cursor_path),
            "--apply",
            "--full-rebuild",
        ]
    ) == 0

    project = next(
        item
        for item in projection_api.list_projects()
        if item["id"] == "captain-runtime-projection-v2"
    )
    posts = projection_api.list_posts(project["id"])
    retired = next(post for post in posts if post["id"] == legacy["marked_id"])
    active_v2 = [post for post in posts if "captain-projection:v2" in post["tags"]]
    store = ProjectionCursorStore(cursor_path)
    assert retired["status"] == "closed"
    assert "captain-projection-retired:v2" in retired["tags"]
    assert len(active_v2) == 1
    assert store.processed_count() == 1
    assert store.get_feed_cursor() == "after-v2-cutover"
    assert store.get_contract_version() == "v2"

    from src import main as minibook_main
    from src.models import Comment, GitHubWebhook, Post, ProjectMember, Webhook

    with minibook_main.SessionLocal() as db:
        assert db.query(Post).filter(Post.id == legacy["human_id"]).one().project_id == legacy["legacy_id"]
        assert db.query(Comment).filter(Comment.id == legacy["comment_id"]).one().post_id == legacy["marked_id"]
        assert db.query(ProjectMember).filter(ProjectMember.id == legacy["membership_id"]).one().project_id == legacy["legacy_id"]
        assert db.query(Webhook).filter(Webhook.id == legacy["webhook_id"]).one().project_id == legacy["legacy_id"]
        assert db.query(GitHubWebhook).filter(GitHubWebhook.id == legacy["github_id"]).one().project_id == legacy["legacy_id"]


def test_interrupted_real_v1_cutover_keeps_cursor_and_restart_converges(
    tmp_path: Path,
    projection_api: MinibookClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    legacy = _seed_real_v1_project()
    cursor_path = tmp_path / "cursor.db"
    ProjectionCursorStore(cursor_path).set_feed_cursor("legacy-position")
    monkeypatch.setenv("CAPTAIN_GATEWAY_TOKEN", "test-only")
    monkeypatch.setenv("MINIBOOK_API_KEY", "test-only")
    monkeypatch.setenv("MINIBOOK_PROJECTION_API_KEY", PROJECTION_API_KEY)
    monkeypatch.setattr(
        rebuild_script,
        "CaptainProjectionFeed",
        lambda *a, **k: _single_page_feed([document], cursor="after-restart"),
    )
    monkeypatch.setattr(rebuild_script, "MinibookClient", lambda *a, **k: projection_api)
    original_retire = projection_api.retire_projection_post

    def abort_retirement(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("injected retirement abort")

    monkeypatch.setattr(projection_api, "retire_projection_post", abort_retirement)
    argv = [
        "--captain-url",
        "https://captain.test",
        "--minibook-url",
        "http://127.0.0.1",
        "--cursor-db",
        str(cursor_path),
        "--apply",
        "--full-rebuild",
    ]
    with pytest.raises(RuntimeError, match="injected retirement abort"):
        rebuild_script.main(argv)

    interrupted = ProjectionCursorStore(cursor_path)
    assert interrupted.get_feed_cursor() == "legacy-position"
    assert interrupted.get_contract_version() is None
    monkeypatch.setattr(projection_api, "retire_projection_post", original_retire)

    assert rebuild_script.main(argv) == 0
    recovered = ProjectionCursorStore(cursor_path)
    assert recovered.get_feed_cursor() == "after-restart"
    assert recovered.get_contract_version() == "v2"
    fixed_posts = projection_api.list_posts("captain-runtime-projection-v2")
    assert next(post for post in fixed_posts if post["id"] == legacy["marked_id"])["status"] == "closed"
