from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from typing import Any, Iterable, Literal
from uuid import uuid4

from .minibook_client import (
    MinibookClient,
    RemoteProjectionConflict,
    RemoteProjectionStale,
)
from .minibook_events import MinibookProjectionEvent
from .projection_cursor import ProjectionCursorStore, projection_event_fingerprint


ProjectionOutcome = Literal["projected", "duplicate", "quarantined", "busy"]


@dataclass(frozen=True)
class ProjectionResult:
    event_id: str
    outcome: ProjectionOutcome
    post_id: str | None = None


@dataclass(frozen=True)
class DeduplicatedProjectionEvents:
    events: tuple[MinibookProjectionEvent, ...]
    quarantined: tuple[ProjectionResult, ...]
    conflicting_event_ids: tuple[str, ...]


class ConflictingProjectionEvent(ValueError):
    """Raised when one authoritative page assigns different data to one event ID."""

    def __init__(self, event_ids: Iterable[str]) -> None:
        self.event_ids = tuple(event_ids)
        super().__init__(
            "conflicting authoritative projection event IDs: "
            + ", ".join(self.event_ids)
        )


@dataclass(frozen=True)
class ProjectionPost:
    title: str
    content: str
    tags: tuple[str, ...]
    content_hash: str


@dataclass(frozen=True)
class DriftReport:
    missing_event_ids: tuple[str, ...] = ()
    modified_event_ids: tuple[str, ...] = ()
    duplicate_event_ids: tuple[str, ...] = ()
    duplicate_post_ids: tuple[str, ...] = ()
    orphaned_post_ids: tuple[str, ...] = ()
    legacy_v1_post_ids: tuple[str, ...] = ()
    changes_applied: int = 0

    @property
    def total_changes(self) -> int:
        return (
            len(self.missing_event_ids)
            + len(self.modified_event_ids)
            + len(self.duplicate_post_ids)
            + len(self.orphaned_post_ids)
            + len(self.legacy_v1_post_ids)
        )


class MinibookProjector:
    PROJECTION_PROJECT_ID = "captain-runtime-projection-v2"
    PROJECTION_PROJECT = "Captain Runtime Projection"
    PROJECTION_DESCRIPTION = (
        "Rebuildable, redacted collaboration views from committed Captain events."
    )

    def __init__(
        self,
        client: MinibookClient,
        cursor_store: ProjectionCursorStore | None = None,
        *,
        claim_ttl: timedelta = timedelta(seconds=30),
        owner_id: str | None = None,
    ) -> None:
        self.client = client
        self.cursor_store = cursor_store
        self.claim_ttl = claim_ttl
        self.owner_id = owner_id or str(uuid4())

    def ensure_project(self, name: str, description: str) -> dict[str, Any]:
        existing = next(
            (item for item in self.client.list_projects() if item["name"] == name),
            None,
        )
        return existing or self.client.create_project(name, description)

    def ensure_projection_project(self) -> dict[str, Any]:
        return self.client.ensure_projection_project(
            external_id=self.PROJECTION_PROJECT_ID,
        )

    def project(self, event: MinibookProjectionEvent) -> ProjectionResult:
        event = self._validated_event(event)
        store = self._require_cursor_store()
        event_id = str(event.event_id)
        claim = store.claim_event(
            event,
            owner_id=self.owner_id,
            ttl=self.claim_ttl,
        )
        if claim.outcome == "duplicate":
            return ProjectionResult(event_id=event_id, outcome="duplicate")
        if claim.outcome == "conflict":
            store.quarantine(
                event,
                reason="conflicting_duplicate_event_id",
                retryable=False,
            )
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        if claim.outcome == "unverifiable":
            store.quarantine(
                event,
                reason="unverifiable_legacy_event_fingerprint",
                retryable=False,
            )
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        if claim.outcome == "stale":
            store.quarantine(event, reason="stale_subject_version")
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        if claim.outcome == "busy":
            return ProjectionResult(event_id=event_id, outcome="busy")

        try:
            project = self.ensure_projection_project()
            desired = self.render(event)
            post = self.client.upsert_projection_post(project["id"], event=event)
        except RemoteProjectionStale:
            store.release_claim(event_id, owner_id=self.owner_id)
            store.quarantine(event, reason="remote_stale_subject_version")
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        except RemoteProjectionConflict:
            store.release_claim(event_id, owner_id=self.owner_id)
            store.quarantine(
                event,
                reason="remote_projection_conflict",
                retryable=False,
            )
            return ProjectionResult(event_id=event_id, outcome="quarantined")
        except BaseException:
            store.release_claim(event_id, owner_id=self.owner_id)
            raise
        store.complete_claim(
            event,
            owner_id=self.owner_id,
            post_id=post["id"],
            content_hash=desired.content_hash,
        )
        return ProjectionResult(
            event_id=event_id,
            outcome="projected",
            post_id=post["id"],
        )

    def deduplicate_events(
        self,
        events: Iterable[MinibookProjectionEvent],
        *,
        quarantine_conflicts: bool,
    ) -> DeduplicatedProjectionEvents:
        unique: dict[str, tuple[str, MinibookProjectionEvent]] = {}
        conflicted: dict[str, MinibookProjectionEvent] = {}
        for candidate in events:
            event = self._validated_event(candidate)
            event_id = str(event.event_id)
            fingerprint = projection_event_fingerprint(event)
            prior = unique.get(event_id)
            if prior is None and event_id not in conflicted:
                unique[event_id] = (fingerprint, event)
                continue
            if prior is not None and prior[0] == fingerprint:
                continue
            if prior is not None:
                conflicted[event_id] = prior[1]
                del unique[event_id]

        quarantined: list[ProjectionResult] = []
        if quarantine_conflicts:
            store = self._require_cursor_store()
            for event_id, event in conflicted.items():
                store.quarantine(
                    event,
                    reason="conflicting_duplicate_event_id",
                    retryable=False,
                )
                quarantined.append(
                    ProjectionResult(event_id=event_id, outcome="quarantined")
                )
        return DeduplicatedProjectionEvents(
            events=tuple(item[1] for item in unique.values()),
            quarantined=tuple(quarantined),
            conflicting_event_ids=tuple(conflicted),
        )

    def rebuild(
        self, events: Iterable[MinibookProjectionEvent]
    ) -> list[ProjectionResult]:
        return [self.project(event) for event in events]

    def reconcile(
        self,
        events: Iterable[MinibookProjectionEvent],
        *,
        apply: bool = False,
    ) -> DriftReport:
        deduplicated = self.deduplicate_events(
            events,
            quarantine_conflicts=apply,
        )
        if deduplicated.conflicting_event_ids:
            raise ConflictingProjectionEvent(deduplicated.conflicting_event_ids)
        authoritative_events = list(deduplicated.events)
        existing_project = next(
            (
                item
                for item in self.client.list_projects()
                if item["name"] == self.PROJECTION_PROJECT
            ),
            None,
        )
        if existing_project is None and not apply:
            return DriftReport(
                missing_event_ids=tuple(str(event.event_id) for event in authoritative_events)
            )
        project = existing_project or self.ensure_projection_project()
        all_active_posts = [
            post
            for post in self.client.list_posts(project["id"])
            if post.get("status") == "open"
        ]
        active_posts = [
            post
            for post in all_active_posts
            if "captain-projection:v2" in post.get("tags", [])
        ]
        legacy_v1_posts = [
            post
            for post in all_active_posts
            if "captain-projection:v1" in post.get("tags", [])
        ]
        expected_ids = {str(event.event_id) for event in authoritative_events}
        missing: list[str] = []
        modified: list[str] = []
        duplicate_events: list[str] = []
        duplicate_posts: list[dict[str, Any]] = []
        canonical_posts: dict[str, dict[str, Any]] = {}
        events_by_id: dict[str, MinibookProjectionEvent] = {}

        for event in authoritative_events:
            event_id = str(event.event_id)
            desired = self.render(event)
            events_by_id[event_id] = event
            event_tag = f"captain-event:{event_id}"
            matches = [post for post in active_posts if event_tag in post.get("tags", [])]
            if not matches:
                missing.append(event_id)
                continue
            canonical = next(
                (post for post in matches if self._post_matches(post, desired)),
                matches[0],
            )
            canonical_posts[event_id] = canonical
            if not self._post_matches(canonical, desired):
                modified.append(event_id)
            extras = [post for post in matches if post["id"] != canonical["id"]]
            if extras:
                duplicate_events.append(event_id)
                duplicate_posts.extend(extras)

        duplicate_ids = {post["id"] for post in duplicate_posts}
        orphaned_posts = [
            post
            for post in active_posts
            if post["id"] not in duplicate_ids
            and not any(
                tag.removeprefix("captain-event:") in expected_ids
                for tag in post.get("tags", [])
                if tag.startswith("captain-event:")
            )
        ]

        changes_applied = 0
        if apply:
            for event_id in missing:
                self._upsert_projection_event(
                    project["id"],
                    events_by_id[event_id],
                )
                changes_applied += 1
            for event_id in modified:
                self._upsert_projection_event(
                    project["id"],
                    events_by_id[event_id],
                )
                changes_applied += 1
            for post in duplicate_posts:
                self._retire_projection_post(post, reason="duplicate")
                changes_applied += 1
            for post in orphaned_posts:
                self._retire_projection_post(post, reason="orphaned")
                changes_applied += 1
            for post in legacy_v1_posts:
                self._retire_projection_post(post, reason="v1-cutover")
                changes_applied += 1

        return DriftReport(
            missing_event_ids=tuple(missing),
            modified_event_ids=tuple(modified),
            duplicate_event_ids=tuple(duplicate_events),
            duplicate_post_ids=tuple(post["id"] for post in duplicate_posts),
            orphaned_post_ids=tuple(post["id"] for post in orphaned_posts),
            legacy_v1_post_ids=tuple(post["id"] for post in legacy_v1_posts),
            changes_applied=changes_applied,
        )

    def render(self, event: MinibookProjectionEvent) -> ProjectionPost:
        payload = event.payload
        fields: list[tuple[str, str]] = [
            ("Status", self._status_label(payload.status_id)),
            ("View", self._view_label(payload.view)),
            ("Correlation", str(event.correlation_id)),
            ("Subject", event.subject_id),
            ("Subject version", str(event.subject_version)),
        ]
        if payload.batch_id is not None:
            fields.append(("Batch", payload.batch_id))
        if payload.batch_version is not None:
            fields.append(("Batch version", str(payload.batch_version)))
        if payload.actor_role_id is not None:
            fields.append(("Actor", self._actor_label(payload.actor_role_id)))
        if payload.artifact_digest is not None:
            fields.append(("Artifact", payload.artifact_digest))
        content = "\n".join(f"- **{label}:** {value}" for label, value in fields)
        title = f"[{event.event_type}] {self._template_title(payload.template_id)}"
        identity_tags = (
            "captain-projection:v2",
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
    def _template_title(template_id: str) -> str:
        return {
            "runtime_plan_requested": "Runtime planning requested",
            "runtime_plan_published": "Runtime delivery plan published",
            "runtime_blueprint_published": "Runtime blueprint published",
            "runtime_build_running": "Runtime build running",
            "runtime_build_recorded": "Runtime build result recorded",
            "automation_evidence_recorded": "Automation evidence recorded",
            "runtime_validation_recorded": "Runtime validation recorded",
            "runtime_replanning_requested": "Runtime replanning requested",
        }[template_id]

    @staticmethod
    def _status_label(status_id: str) -> str:
        return {
            "requested": "Requested",
            "planned": "Planned",
            "ready": "Ready",
            "running": "Running",
            "built": "Built",
            "observed": "Observed",
            "validated": "Validated",
            "replanning": "Replanning",
        }[status_id]

    @staticmethod
    def _view_label(view: str) -> str:
        return {
            "project": "Project",
            "plan": "Plan",
            "blueprint": "Blueprint",
            "build": "Build",
            "validation": "Validation",
        }[view]

    @staticmethod
    def _actor_label(actor_role_id: str) -> str:
        return {
            "captain_planner": "Captain Planner",
            "codex_worker": "Codex Worker",
        }[actor_role_id]

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

    def _upsert_projection_event(
        self,
        project_id: str,
        event: MinibookProjectionEvent,
    ) -> dict[str, Any]:
        return self.client.upsert_projection_post(project_id, event=event)

    def _retire_projection_post(self, post: dict[str, Any], *, reason: str) -> None:
        tags = [
            tag
            for tag in post.get("tags", [])
            if tag not in {"captain-projection:v1", "captain-projection:v2"}
        ]
        tags.extend(("captain-projection-retired:v2", f"captain-retired:{reason}"))
        self.client.update_post(post["id"], status="closed", tags=sorted(set(tags)))

    def _require_cursor_store(self) -> ProjectionCursorStore:
        if self.cursor_store is None:
            raise RuntimeError("projection cursor store is required for event projection")
        return self.cursor_store

    @staticmethod
    def _validated_event(event: MinibookProjectionEvent) -> MinibookProjectionEvent:
        document = dict(event.__dict__)
        document["payload"] = dict(event.payload.__dict__)
        return MinibookProjectionEvent.model_validate(document)

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
