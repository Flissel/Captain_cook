from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from .minibook_events import MinibookProjectionEvent


@dataclass(frozen=True)
class QuarantineRecord:
    event_id: str
    subject_id: str
    subject_version: int
    reason: str
    retryable: bool


class StaleProjectionVersion(RuntimeError):
    pass


class ProjectionCursorStore:
    """Durable local identity/cursor metadata for a disposable projection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
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
                """
            )

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

    def set_feed_cursor(self, cursor: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO projection_state(key, value) VALUES('feed_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (cursor,),
            )

    def commit_event(
        self,
        event: MinibookProjectionEvent,
        *,
        post_id: str,
        content_hash: str,
        feed_cursor: str | None = None,
    ) -> None:
        event_id = str(event.event_id)
        committed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM processed_projection_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if feed_cursor is not None:
                    self._set_feed_cursor(connection, feed_cursor)
                return
            head = connection.execute(
                "SELECT subject_version FROM projection_subject_heads WHERE subject_id = ?",
                (event.subject_id,),
            ).fetchone()
            if head is not None and event.subject_version <= int(head["subject_version"]):
                raise StaleProjectionVersion(event.subject_id)
            connection.execute(
                """
                INSERT INTO processed_projection_events(
                    event_id, subject_id, subject_version, post_id, content_hash, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event.subject_id,
                    event.subject_version,
                    post_id,
                    content_hash,
                    committed_at,
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
            if feed_cursor is not None:
                self._set_feed_cursor(connection, feed_cursor)

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
                    datetime.now(timezone.utc).isoformat(),
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

    @staticmethod
    def _set_feed_cursor(connection: sqlite3.Connection, cursor: str) -> None:
        connection.execute(
            """
            INSERT INTO projection_state(key, value) VALUES('feed_cursor', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (cursor,),
        )
