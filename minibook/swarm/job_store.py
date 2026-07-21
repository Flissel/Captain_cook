"""Append-only SQLite persistence for resumable creation jobs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import (
    CreationJobV1,
    CreationProgressV1,
    CreationResultV1,
    CreationSubmissionReceipt,
)


class CreationConflictError(RuntimeError):
    pass


class CreationNotFoundError(KeyError):
    pass


def _json(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", by_alias=True)  # type: ignore[union-attr]
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CreationJobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS creation_jobs (
                    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creation_heads (
                    job_id TEXT NOT NULL, version INTEGER NOT NULL,
                    status TEXT NOT NULL, checkpoint TEXT, snapshot TEXT NOT NULL,
                    PRIMARY KEY (job_id, version)
                );
                CREATE TABLE IF NOT EXISTS creation_step_receipts (
                    job_id TEXT NOT NULL, step TEXT NOT NULL, effect_key TEXT NOT NULL,
                    snapshot TEXT NOT NULL, PRIMARY KEY (job_id, step)
                );
                CREATE TABLE IF NOT EXISTS creation_effect_receipts (
                    job_id TEXT NOT NULL, effect_key TEXT NOT NULL, receipt TEXT NOT NULL,
                    PRIMARY KEY (job_id, effect_key)
                );
                CREATE TABLE IF NOT EXISTS creation_results (
                    job_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                """
            )

    def submit(self, job: CreationJobV1) -> CreationSubmissionReceipt:
        job_id = str(job.creation_job_id)
        payload = _json(job)
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM creation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != payload:
                    raise CreationConflictError("creation job id already has different content")
                return CreationSubmissionReceipt(
                    creation_job_id=job.creation_job_id,
                    status="queued",
                    subject_version=job.subject_version,
                    replayed=True,
                )
            db.execute("INSERT INTO creation_jobs VALUES (?, ?)", (job_id, payload))
            db.execute(
                "INSERT INTO creation_heads VALUES (?, 1, 'queued', NULL, '{}')",
                (job_id,),
            )
        return CreationSubmissionReceipt(
            creation_job_id=job.creation_job_id,
            status="queued",
            subject_version=job.subject_version,
        )

    def job(self, job_id: UUID) -> CreationJobV1:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM creation_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise CreationNotFoundError(str(job_id))
        return CreationJobV1.model_validate_json(row["payload"])

    def _head(self, job_id: UUID, db: sqlite3.Connection | None = None) -> sqlite3.Row:
        owns = db is None
        connection = db or self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM creation_heads WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
        finally:
            if owns:
                connection.close()
        if row is None:
            raise CreationNotFoundError(str(job_id))
        return row

    def progress(self, job_id: UUID) -> CreationProgressV1:
        job = self.job(job_id)
        head = self._head(job_id)
        return CreationProgressV1(
            creation_job_id=job_id,
            subject_version=job.subject_version,
            attempt=job.attempt,
            status=head["status"],
            checkpoint=head["checkpoint"],
            version=head["version"],
        )

    def snapshot(self, job_id: UUID) -> dict[str, Any]:
        return json.loads(self._head(job_id)["snapshot"])

    def completed_steps(self, job_id: UUID) -> set[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT step FROM creation_step_receipts WHERE job_id = ?", (str(job_id),)
            ).fetchall()
        return {row["step"] for row in rows}

    def external_effect(self, job_id: UUID, effect_key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT receipt FROM creation_effect_receipts WHERE job_id = ? AND effect_key = ?",
                (str(job_id), effect_key),
            ).fetchone()
        return None if row is None else json.loads(row["receipt"])

    def record_external_effect(
        self, job_id: UUID, effect_key: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        encoded = _json(receipt)
        with self._connect() as db:
            row = db.execute(
                "SELECT receipt FROM creation_effect_receipts WHERE job_id = ? AND effect_key = ?",
                (str(job_id), effect_key),
            ).fetchone()
            if row is not None:
                if row["receipt"] != encoded:
                    raise CreationConflictError("effect key already has different receipt")
                return json.loads(row["receipt"])
            db.execute(
                "INSERT INTO creation_effect_receipts VALUES (?, ?, ?)",
                (str(job_id), effect_key, encoded),
            )
        return receipt

    def complete_step(
        self, job_id: UUID, step: str, effect_key: str, snapshot: dict[str, Any]
    ) -> None:
        encoded = _json(snapshot)
        with self._connect() as db:
            existing = db.execute(
                "SELECT snapshot FROM creation_step_receipts WHERE job_id = ? AND step = ?",
                (str(job_id), step),
            ).fetchone()
            if existing is not None:
                if existing["snapshot"] != encoded:
                    raise CreationConflictError("step already has different receipt")
                return
            head = self._head(job_id, db)
            db.execute(
                "INSERT INTO creation_step_receipts VALUES (?, ?, ?, ?)",
                (str(job_id), step, effect_key, encoded),
            )
            db.execute(
                "INSERT INTO creation_heads VALUES (?, ?, 'running', ?, ?)",
                (str(job_id), head["version"] + 1, step, encoded),
            )

    def cancel(self, job_id: UUID, expected_version: int) -> CreationProgressV1:
        with self._connect() as db:
            head = self._head(job_id, db)
            if head["version"] != expected_version:
                raise CreationConflictError("creation job version changed")
            if head["status"] in {"succeeded", "failed", "blocked", "cancelled"}:
                raise CreationConflictError("creation job is already terminal")
            db.execute(
                "INSERT INTO creation_heads VALUES (?, ?, 'cancelled', ?, ?)",
                (str(job_id), head["version"] + 1, head["checkpoint"], head["snapshot"]),
            )
        return self.progress(job_id)

    def finish(self, result: CreationResultV1) -> CreationResultV1:
        encoded = _json(result)
        job_id = str(result.creation_job_id)
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM creation_results WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CreationConflictError("creation result already differs")
                return CreationResultV1.model_validate_json(existing["payload"])
            head = self._head(result.creation_job_id, db)
            db.execute("INSERT INTO creation_results VALUES (?, ?)", (job_id, encoded))
            db.execute(
                "INSERT INTO creation_heads VALUES (?, ?, ?, ?, ?)",
                (job_id, head["version"] + 1, result.status, head["checkpoint"], head["snapshot"]),
            )
        return result

    def result(self, job_id: UUID) -> CreationResultV1 | None:
        self.job(job_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM creation_results WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        return None if row is None else CreationResultV1.model_validate_json(row["payload"])
