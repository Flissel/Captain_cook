from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .models import (
    DeliveryEvent,
    DeliveryEvidence,
    DeliveryRole,
    DeliveryStatus,
    DeliveryTodo,
    utc_now,
)
from .state_machine import DeliveryTransitionError, resolved_target, validate_transition


class SqliteDeliveryLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS delivery_todos (
                    todo_id TEXT PRIMARY KEY,
                    document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delivery_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    todo_id TEXT NOT NULL REFERENCES delivery_todos(todo_id),
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_delivery_event_id
                    ON delivery_events(event_id);
                """
            )

    def create_todo(
        self,
        project_id: str,
        title: str,
        description: str,
        acceptance_criteria: tuple[str, ...],
        *,
        dependencies: tuple[str, ...] = (),
        todo_id: str | None = None,
        event_id: str | None = None,
    ) -> DeliveryTodo:
        todo = DeliveryTodo(
            todo_id=todo_id or str(uuid4()),
            project_id=project_id,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            dependencies=dependencies,
        )
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO delivery_todos(todo_id, document) VALUES (?, ?)",
                (todo.todo_id, todo.model_dump_json()),
            )
            self._insert_event(
                connection,
                event_id or f"todo-created:{todo.todo_id}",
                todo.todo_id,
                "captain",
                "todo_created",
                {"version": todo.version},
            )
        return todo

    def get_todo(self, todo_id: str) -> DeliveryTodo:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM delivery_todos WHERE todo_id = ?", (todo_id,)
            ).fetchone()
        if row is None:
            raise KeyError(todo_id)
        return DeliveryTodo.model_validate_json(row["document"])

    def list_todos(
        self,
        *,
        assignee: DeliveryRole | None = None,
        status: DeliveryStatus | None = None,
    ) -> tuple[DeliveryTodo, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document FROM delivery_todos ORDER BY rowid"
            ).fetchall()
        todos = tuple(DeliveryTodo.model_validate_json(row["document"]) for row in rows)
        return tuple(
            todo
            for todo in todos
            if (assignee is None or todo.assignee is assignee)
            and (status is None or todo.status is status)
        )

    def transition(
        self,
        todo_id: str,
        event_id: str,
        expected_version: int,
        actor: str,
        target: DeliveryStatus,
        *,
        assignee: DeliveryRole | None = None,
    ) -> DeliveryTodo:
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT todo_id FROM delivery_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if duplicate is not None:
                if duplicate["todo_id"] != todo_id:
                    raise DeliveryTransitionError("event_id belongs to another TODO")
                return self._get_todo_in(connection, todo_id)

            current = self._get_todo_in(connection, todo_id)
            if current.version != expected_version:
                raise DeliveryTransitionError(
                    f"stale version {expected_version}; current is {current.version}"
                )
            validate_transition(current, actor, target, assignee)
            final_target = resolved_target(current, target)
            next_iteration = current.iteration
            if final_target is DeliveryStatus.REDO:
                next_iteration += 1
            now = utc_now()
            updated = current.model_copy(
                update={
                    "status": final_target,
                    "assignee": assignee if current.status is DeliveryStatus.PLANNED else current.assignee,
                    "iteration": next_iteration,
                    "version": current.version + 1,
                    "updated_at": now,
                }
            )
            connection.execute(
                "UPDATE delivery_todos SET document = ? WHERE todo_id = ?",
                (updated.model_dump_json(), todo_id),
            )
            self._insert_event(
                connection,
                event_id,
                todo_id,
                actor,
                "status_transitioned",
                {
                    "from": current.status.value,
                    "to": final_target.value,
                    "version": updated.version,
                },
            )
            return updated

    def append_evidence(
        self,
        todo_id: str,
        event_id: str,
        expected_version: int,
        actor: str,
        evidence: DeliveryEvidence,
    ) -> DeliveryTodo:
        with self._transaction() as connection:
            duplicate = connection.execute(
                "SELECT todo_id FROM delivery_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if duplicate is not None:
                return self._get_todo_in(connection, todo_id)
            current = self._get_todo_in(connection, todo_id)
            if current.version != expected_version:
                raise DeliveryTransitionError("stale version")
            if current.assignee is None or actor != current.assignee.value:
                raise DeliveryTransitionError("only the assigned role may append evidence")
            updated = current.model_copy(
                update={
                    "evidence": (*current.evidence, evidence),
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                }
            )
            connection.execute(
                "UPDATE delivery_todos SET document = ? WHERE todo_id = ?",
                (updated.model_dump_json(), todo_id),
            )
            self._insert_event(
                connection, event_id, todo_id, actor, "evidence_appended",
                {"evidence_id": evidence.evidence_id, "version": updated.version},
            )
            return updated

    def events_after(self, sequence: int) -> tuple[DeliveryEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_events WHERE sequence > ? ORDER BY sequence",
                (sequence,),
            ).fetchall()
        return tuple(
            DeliveryEvent(
                sequence=row["sequence"],
                event_id=row["event_id"],
                todo_id=row["todo_id"],
                actor=row["actor"],
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    def _get_todo_in(
        self, connection: sqlite3.Connection, todo_id: str
    ) -> DeliveryTodo:
        row = connection.execute(
            "SELECT document FROM delivery_todos WHERE todo_id = ?", (todo_id,)
        ).fetchone()
        if row is None:
            raise KeyError(todo_id)
        return DeliveryTodo.model_validate_json(row["document"])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        event_id: str,
        todo_id: str,
        actor: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            """INSERT INTO delivery_events
               (event_id, todo_id, actor, event_type, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, todo_id, actor, event_type, json.dumps(payload), utc_now().isoformat()),
        )
