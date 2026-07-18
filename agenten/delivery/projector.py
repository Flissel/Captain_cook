from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Literal

from .minibook_client import MinibookClient
from .minibook_events import MinibookProjectionEvent
from .projection_cursor import ProjectionCursorStore, StaleProjectionVersion


ProjectionOutcome = Literal["projected", "duplicate", "quarantined"]


@dataclass(frozen=True)
class ProjectionResult:
    event_id: str
    outcome: ProjectionOutcome
    post_id: str | None = None


@dataclass(frozen=True)
class ProjectionPost:
    title: str
    content: str
    tags: tuple[str, ...]
    content_hash: str


class MinibookProjector:
    PROJECTION_PROJECT = "Captain Runtime Projection"
    PROJECTION_DESCRIPTION = (
        "Rebuildable, redacted collaboration views from committed Captain events."
    )

    def __init__(
        self,
        client: MinibookClient,
        cursor_store: ProjectionCursorStore | None = None,
    ) -> None:
        self.client = client
        self.cursor_store = cursor_store

    def ensure_project(self, name: str, description: str) -> dict[str, Any]:
        existing = next(
            (item for item in self.client.list_projects() if item["name"] == name),
            None,
        )
        return existing or self.client.create_project(name, description)

    def ensure_projection_project(self) -> dict[str, Any]:
        return self.ensure_project(self.PROJECTION_PROJECT, self.PROJECTION_DESCRIPTION)

    def project(
        self,
        event: MinibookProjectionEvent,
        *,
        feed_cursor: str | None = None,
    ) -> ProjectionResult:
        store = self._require_cursor_store()
        event_id = str(event.event_id)
        if store.is_processed(event_id):
            if feed_cursor is not None:
                store.set_feed_cursor(feed_cursor)
            return ProjectionResult(event_id=event_id, outcome="duplicate")

        subject_version = store.subject_version(event.subject_id)
        if subject_version is not None and event.subject_version <= subject_version:
            store.quarantine(event, reason="stale_subject_version")
            return ProjectionResult(event_id=event_id, outcome="quarantined")

        project = self.ensure_projection_project()
        desired = self.render(event)
        event_tag = f"captain-event:{event_id}"
        matches = self.client.search_posts(project_id=project["id"], tag=event_tag)
        if matches:
            post = matches[0]
            if not self._post_matches(post, desired):
                post = self.client.update_post(
                    post["id"],
                    title=desired.title,
                    content=desired.content,
                    status="open",
                    tags=list(desired.tags),
                )
        else:
            post = self.client.create_post(
                project["id"],
                title=desired.title,
                content=desired.content,
                tags=list(desired.tags),
            )
        try:
            store.commit_event(
                event,
                post_id=post["id"],
                content_hash=desired.content_hash,
                feed_cursor=feed_cursor,
            )
        except StaleProjectionVersion:
            store.quarantine(event, reason="stale_subject_version")
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        return ProjectionResult(
            event_id=event_id,
            outcome="projected",
            post_id=post["id"],
        )

    def rebuild(
        self, events: Iterable[MinibookProjectionEvent]
    ) -> list[ProjectionResult]:
        return [self.project(event) for event in events]

    def render(self, event: MinibookProjectionEvent) -> ProjectionPost:
        payload = event.payload
        fields: list[tuple[str, str]] = [
            ("Status", payload.status),
            ("View", payload.view),
            ("Correlation", str(event.correlation_id)),
            ("Subject", event.subject_id),
            ("Subject version", str(event.subject_version)),
        ]
        if payload.batch_id is not None:
            fields.append(("Batch", payload.batch_id))
        if payload.batch_version is not None:
            fields.append(("Batch version", str(payload.batch_version)))
        if payload.assignee_display_name is not None:
            fields.append(("Assignee", payload.assignee_display_name))
        if payload.artifact_digest is not None:
            fields.append(("Artifact", payload.artifact_digest))
        if payload.evidence_summary is not None:
            fields.append(("Evidence", payload.evidence_summary))
        content = "\n".join(f"- **{label}:** {value}" for label, value in fields)
        title = f"[{event.event_type}] {payload.public_title}"
        identity_tags = (
            "captain-projection:v1",
            f"captain-event:{event.event_id}",
            f"captain-correlation:{event.correlation_id}",
            f"captain-subject:{event.subject_id}",
            f"captain-version:{event.subject_version}",
            f"captain-view:{payload.view}",
        )
        content_hash = self._content_hash(title, content, identity_tags)
        return ProjectionPost(
            title=title,
            content=content,
            tags=(*identity_tags, f"captain-hash:{content_hash}"),
            content_hash=content_hash,
        )

    @staticmethod
    def _content_hash(title: str, content: str, tags: tuple[str, ...]) -> str:
        canonical = json.dumps(
            {"title": title, "content": content, "tags": tags},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _post_matches(post: dict[str, Any], desired: ProjectionPost) -> bool:
        return (
            post.get("title") == desired.title
            and post.get("content") == desired.content
            and post.get("status") == "open"
            and set(post.get("tags", [])) == set(desired.tags)
        )

    def _require_cursor_store(self) -> ProjectionCursorStore:
        if self.cursor_store is None:
            raise RuntimeError("projection cursor store is required for event projection")
        return self.cursor_store

    def upsert_plan(self, project_id: str, title: str, content: str) -> dict[str, Any]:
        existing = next(
            (post for post in self.client.list_posts(project_id) if post["title"] == title),
            None,
        )
        if existing:
            return self.client.update_post(
                existing["id"],
                content=content,
                status="open",
                pin_order=0,
                tags=["captain-delivery-plan"],
            )
        post = self.client.create_post(
            project_id,
            title=title,
            content=content,
            tags=["captain-delivery-plan"],
        )
        return self.client.update_post(post["id"], pin_order=0)

    def post_assignment(
        self, post_id: str, *, run_id: str, assignee: str, todo_title: str
    ) -> dict[str, Any]:
        return self.client.create_comment(
            post_id,
            f"Assignment `{run_id}` → **{assignee}**: {todo_title}",
        )
