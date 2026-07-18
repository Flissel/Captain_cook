from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from .minibook_events import MinibookProjectionEvent


ClaimOutcome = Literal[
    "acquired",
    "duplicate",
    "stale",
    "busy",
    "conflict",
    "unverifiable",
]


@dataclass(frozen=True)
class ClaimResult:
    outcome: ClaimOutcome


@dataclass(frozen=True)
class QuarantineRecord:
    event_id: str
    subject_id: str
    subject_version: int
    reason: str
    retryable: bool


class StaleProjectionVersion(RuntimeError):
    pass


class LostProjectionClaim(RuntimeError):
    pass


def projection_event_fingerprint(event: MinibookProjectionEvent) -> str:
    canonical = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProjectionCursorStore:
    """Durable local identity, claim, cursor, and quarantine metadata."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_projection_events (
                    event_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    subject_version INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    event_fingerprint TEXT NOT NULL DEFAULT '',
                    committed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_subject_heads (
                    subject_id TEXT PRIMARY KEY,
                    subject_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_quarantine (
                    event_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    subject_version INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    retryable INTEGER NOT NULL,
                    quarantined_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_event_claims (
                    event_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    subject_version INTEGER NOT NULL,
                    event_fingerprint TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_subject_claims (
                    subject_id TEXT PRIMARY KEY,
                    subject_version INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(processed_projection_events)"
                ).fetchall()
            }
            if "event_fingerprint" not in columns:
                connection.execute(
                    "ALTER TABLE processed_projection_events "
                    "ADD COLUMN event_fingerprint TEXT NOT NULL DEFAULT ''"
                )

    def claim_event(
        self,
        event: MinibookProjectionEvent,
        *,
        owner_id: str,
        ttl: timedelta,
    ) -> ClaimResult:
        now = self._aware_now()
        expires_at = now + ttl
        event_id = str(event.event_id)
        fingerprint = projection_event_fingerprint(event)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                processed = connection.execute(
                    "SELECT event_fingerprint FROM processed_projection_events "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if processed is not None:
                    stored = str(processed["event_fingerprint"])
                    if not stored:
                        outcome: ClaimOutcome = "unverifiable"
                    else:
                        outcome = "conflict" if stored != fingerprint else "duplicate"
                    connection.commit()
                    return ClaimResult(outcome)

                event_claim = connection.execute(
                    "SELECT * FROM projection_event_claims WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if event_claim is not None:
                    if str(event_claim["event_fingerprint"]) != fingerprint:
                        connection.commit()
                        return ClaimResult("conflict")
                    if (
                        str(event_claim["owner_id"]) == owner_id
                        and self._parse_time(str(event_claim["expires_at"])) > now
                    ):
                        connection.commit()
                        return ClaimResult("acquired")
                    if self._parse_time(str(event_claim["expires_at"])) > now:
                        connection.commit()
                        return ClaimResult("busy")
                    self._delete_claim_rows(connection, event_id=event_id)

                head = connection.execute(
                    "SELECT subject_version FROM projection_subject_heads "
                    "WHERE subject_id = ?",
                    (event.subject_id,),
                ).fetchone()
                if head is not None and event.subject_version <= int(head["subject_version"]):
                    connection.commit()
                    return ClaimResult("stale")

                subject_claim = connection.execute(
                    "SELECT * FROM projection_subject_claims WHERE subject_id = ?",
                    (event.subject_id,),
                ).fetchone()
                if subject_claim is not None:
                    if self._parse_time(str(subject_claim["expires_at"])) <= now:
                        self._delete_claim_rows(
                            connection,
                            event_id=str(subject_claim["event_id"]),
                        )
                    else:
                        claimed_version = int(subject_claim["subject_version"])
                        connection.commit()
                        if claimed_version > event.subject_version:
                            return ClaimResult("stale")
                        return ClaimResult("busy")

                connection.execute(
                    """
                    INSERT INTO projection_event_claims(
                        event_id, subject_id, subject_version, event_fingerprint,
                        owner_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event.subject_id,
                        event.subject_version,
                        fingerprint,
                        owner_id,
                        expires_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO projection_subject_claims(
                        subject_id, subject_version, event_id, owner_id, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.subject_id,
                        event.subject_version,
                        event_id,
                        owner_id,
                        expires_at.isoformat(),
                    ),
                )
                connection.commit()
                return ClaimResult("acquired")
            except BaseException:
                connection.rollback()
                raise

    def complete_claim(
        self,
        event: MinibookProjectionEvent,
        *,
        owner_id: str,
        post_id: str,
        content_hash: str,
    ) -> None:
        now = self._aware_now()
        event_id = str(event.event_id)
        fingerprint = projection_event_fingerprint(event)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                claim = connection.execute(
                    "SELECT * FROM projection_event_claims WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if (
                    claim is None
                    or str(claim["owner_id"]) != owner_id
                    or self._parse_time(str(claim["expires_at"])) <= now
                ):
                    raise LostProjectionClaim(event_id)
                head = connection.execute(
                    "SELECT subject_version FROM projection_subject_heads "
                    "WHERE subject_id = ?",
                    (event.subject_id,),
                ).fetchone()
                if head is not None and event.subject_version <= int(head["subject_version"]):
                    raise StaleProjectionVersion(event.subject_id)
                connection.execute(
                    """
                    INSERT INTO processed_projection_events(
                        event_id, subject_id, subject_version, post_id, content_hash,
                        event_fingerprint, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event.subject_id,
                        event.subject_version,
                        post_id,
                        content_hash,
                        fingerprint,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO projection_subject_heads(subject_id, subject_version)
                    VALUES (?, ?)
                    ON CONFLICT(subject_id) DO UPDATE SET
                        subject_version = excluded.subject_version
                    """,
                    (event.subject_id, event.subject_version),
                )
                rebuild = connection.execute(
                    "SELECT value FROM projection_state WHERE key = 'rebuild_in_progress'"
                ).fetchone()
                if rebuild is None:
                    self._set_state(connection, "contract_version", "v2")
                self._delete_claim_rows(connection, event_id=event_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def release_claim(self, event_id: str, *, owner_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                "SELECT owner_id FROM projection_event_claims WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if claim is not None and str(claim["owner_id"]) == owner_id:
                self._delete_claim_rows(connection, event_id=event_id)
            connection.commit()

    def is_processed(self, event_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_projection_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row is not None

    def processed_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM processed_projection_events"
            ).fetchone()
        return int(row["count"])

    def subject_version(self, subject_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT subject_version FROM projection_subject_heads WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        return None if row is None else int(row["subject_version"])

    def get_feed_cursor(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM projection_state WHERE key = 'feed_cursor'"
            ).fetchone()
        return None if row is None else str(row["value"])

    def get_contract_version(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM projection_state WHERE key = 'contract_version'"
            ).fetchone()
        return None if row is None else str(row["value"])

    def validate_incremental_v2_state(self) -> None:
        """Fail closed when an old or interrupted state needs a full rebuild."""

        with self._connect() as connection:
            rebuilding = connection.execute(
                "SELECT value FROM projection_state WHERE key = 'rebuild_in_progress'"
            ).fetchone()
            version = connection.execute(
                "SELECT value FROM projection_state WHERE key = 'contract_version'"
            ).fetchone()
            feed_cursor = connection.execute(
                "SELECT 1 FROM projection_state WHERE key = 'feed_cursor'"
            ).fetchone()
            processed = connection.execute(
                "SELECT 1 FROM processed_projection_events LIMIT 1"
            ).fetchone()
            heads = connection.execute(
                "SELECT 1 FROM projection_subject_heads LIMIT 1"
            ).fetchone()
        if rebuilding is not None:
            raise RuntimeError(
                "projection rebuild was interrupted; run an explicit full rebuild"
            )
        if version is not None and str(version["value"]) != "v2":
            raise RuntimeError("projection contract requires an explicit full rebuild")
        if version is None and any(
            value is not None for value in (feed_cursor, processed, heads)
        ):
            raise RuntimeError(
                "unversioned projection state requires an explicit full rebuild"
            )

    def begin_v2_full_rebuild(self) -> None:
        """Reset only rebuildable local state and leave a crash-detectable marker."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM processed_projection_events")
                connection.execute("DELETE FROM projection_subject_heads")
                connection.execute("DELETE FROM projection_event_claims")
                connection.execute("DELETE FROM projection_subject_claims")
                connection.execute("DELETE FROM projection_quarantine")
                self._set_state(connection, "rebuild_in_progress", "v2")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def checkpoint_v2_feed(self, cursor: str) -> None:
        """Commit terminal v2 cursor/version and clear the rebuild marker atomically."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._set_feed_cursor(connection, cursor)
                self._set_state(connection, "contract_version", "v2")
                connection.execute(
                    "DELETE FROM projection_state WHERE key = 'rebuild_in_progress'"
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def set_feed_cursor(self, cursor: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_feed_cursor(connection, cursor)
            connection.commit()

    def commit_event(
        self,
        event: MinibookProjectionEvent,
        *,
        post_id: str,
        content_hash: str,
        feed_cursor: str | None = None,
    ) -> None:
        owner_id = f"direct-{uuid4()}"
        claim = self.claim_event(
            event,
            owner_id=owner_id,
            ttl=timedelta(minutes=1),
        )
        if claim.outcome == "stale":
            raise StaleProjectionVersion(event.subject_id)
        if claim.outcome == "conflict":
            raise ValueError("conflicting event payload for processed event ID")
        if claim.outcome == "unverifiable":
            raise ValueError("legacy event fingerprint is unverifiable")
        if claim.outcome == "busy":
            raise LostProjectionClaim(str(event.event_id))
        if claim.outcome == "acquired":
            self.complete_claim(
                event,
                owner_id=owner_id,
                post_id=post_id,
                content_hash=content_hash,
            )
        if feed_cursor is not None:
            self.set_feed_cursor(feed_cursor)

    def quarantine(
        self,
        event: MinibookProjectionEvent,
        *,
        reason: str,
        retryable: bool = True,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projection_quarantine(
                    event_id, subject_id, subject_version, reason, retryable, quarantined_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    reason = excluded.reason,
                    retryable = excluded.retryable,
                    quarantined_at = excluded.quarantined_at
                """,
                (
                    str(event.event_id),
                    event.subject_id,
                    event.subject_version,
                    reason,
                    int(retryable),
                    self._aware_now().isoformat(),
                ),
            )

    def list_quarantine(self) -> list[QuarantineRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, subject_id, subject_version, reason, retryable
                FROM projection_quarantine ORDER BY quarantined_at, event_id
                """
            ).fetchall()
        return [
            QuarantineRecord(
                event_id=str(row["event_id"]),
                subject_id=str(row["subject_id"]),
                subject_version=int(row["subject_version"]),
                reason=str(row["reason"]),
                retryable=bool(row["retryable"]),
            )
            for row in rows
        ]

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("projection cursor clock must return an aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    @staticmethod
    def _delete_claim_rows(
        connection: sqlite3.Connection,
        *,
        event_id: str,
    ) -> None:
        connection.execute(
            "DELETE FROM projection_subject_claims WHERE event_id = ?",
            (event_id,),
        )
        connection.execute(
            "DELETE FROM projection_event_claims WHERE event_id = ?",
            (event_id,),
        )

    @staticmethod
    def _set_feed_cursor(connection: sqlite3.Connection, cursor: str) -> None:
        ProjectionCursorStore._set_state(connection, "feed_cursor", cursor)

    @staticmethod
    def _set_state(
        connection: sqlite3.Connection,
        key: str,
        value: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO projection_state(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
