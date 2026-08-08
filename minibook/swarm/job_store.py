"""Append-only SQLite persistence for resumable creation jobs."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import (
    CreationJobV1,
    CreationJobV2,
    CreationJobV3,
    CreationProgressV1,
    CreationResultV1,
    CreationResultV2,
    CreationSubmissionReceipt,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    validate_build_effect_binding,
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
                CREATE TABLE IF NOT EXISTS creation_build_assignments (
                    assignment_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    checkpoint_version INTEGER NOT NULL, effect_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(job_id, checkpoint_version, effect_key)
                );
                CREATE TABLE IF NOT EXISTS creation_assignment_claims (
                    assignment_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creation_build_effects (
                    assignment_id TEXT PRIMARY KEY, effect_key TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(assignment_id, effect_key)
                );
                CREATE TABLE IF NOT EXISTS creation_action_projections (
                    request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def submit(self, job: CreationJobV1 | CreationJobV2 | CreationJobV3) -> CreationSubmissionReceipt:
        job_id = str(job.creation_job_id)
        payload = _json(job)
        initial_snapshot = _json({"creation_job_id": job_id})
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
                "INSERT INTO creation_heads VALUES (?, 1, 'queued', NULL, ?)",
                (job_id, initial_snapshot),
            )
        return CreationSubmissionReceipt(
            creation_job_id=job.creation_job_id,
            status="queued",
            subject_version=job.subject_version,
        )

    def job(self, job_id: UUID) -> CreationJobV1 | CreationJobV2 | CreationJobV3:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM creation_jobs WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        if row is None:
            raise CreationNotFoundError(str(job_id))
        payload = json.loads(row["payload"])
        schema = payload.get("schema")
        parsers = {"minibook.creation-job.v1": CreationJobV1, "minibook.creation-job.v2": CreationJobV2, "minibook.creation-job.v3": CreationJobV3}
        try:
            return parsers[schema].model_validate(payload)
        except KeyError as error:
            raise CreationConflictError("unknown persisted creation job schema") from error

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

    def resumable_job_ids(self) -> tuple[UUID, ...]:
        """Return queued/running jobs in durable submission order."""

        with self._connect() as db:
            rows = db.execute(
                """
                SELECT jobs.job_id
                FROM creation_jobs AS jobs
                JOIN creation_heads AS heads
                  ON heads.job_id = jobs.job_id
                 AND heads.version = (
                    SELECT MAX(current.version)
                    FROM creation_heads AS current
                    WHERE current.job_id = jobs.job_id
                 )
                WHERE heads.status IN ('queued', 'planning', 'awaiting_build', 'building', 'validating', 'packaging', 'readback', 'running')
                ORDER BY jobs.rowid
                """
            ).fetchall()
        return tuple(UUID(row["job_id"]) for row in rows)

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

    def transition(self, job_id: UUID, status: str, *, expected_version: int, checkpoint: str | None = None, snapshot: dict[str, Any] | None = None) -> CreationProgressV1:
        """Append a version-fenced state transition without overwriting evidence."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head = self._head(job_id, db)
            if head["version"] != expected_version:
                raise CreationConflictError("creation job version changed")
            if head["status"] in {"succeeded", "failed", "blocked", "cancelled"}:
                raise CreationConflictError("creation job is already terminal")
            db.execute("INSERT INTO creation_heads VALUES (?, ?, ?, ?, ?)", (str(job_id), expected_version + 1, status, checkpoint if checkpoint is not None else head["checkpoint"], _json(snapshot) if snapshot is not None else head["snapshot"]))
        return self.progress(job_id)

    def publish_build_assignment(self, assignment: FactoryBuildAssignmentV2) -> FactoryBuildAssignmentV2:
        encoded = _json(assignment)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id = ?", (str(assignment.assignment_id),)).fetchone()
            if row is not None:
                if row["payload"] != encoded:
                    raise CreationConflictError("assignment id already has different content")
                return FactoryBuildAssignmentV2.model_validate_json(row["payload"])
            head = self._head(assignment.creation_job_id, db)
            if head["version"] != assignment.checkpoint_version or head["status"] not in {"planning", "awaiting_build"}:
                raise CreationConflictError("assignment checkpoint is no longer current")
            if row is None:
                conflicting = db.execute("SELECT payload FROM creation_build_assignments WHERE job_id=? AND checkpoint_version=? AND effect_key=?", (str(assignment.creation_job_id), assignment.checkpoint_version, assignment.effect_key)).fetchone()
                if conflicting is not None and conflicting["payload"] != encoded:
                    raise CreationConflictError("assignment identity already has different content")
                db.execute("INSERT INTO creation_build_assignments VALUES (?, ?, ?, ?, ?)", (str(assignment.assignment_id), str(assignment.creation_job_id), assignment.checkpoint_version, assignment.effect_key, encoded))
                if head["status"] == "planning":
                    db.execute("INSERT INTO creation_heads VALUES (?, ?, 'awaiting_build', ?, ?)", (str(assignment.creation_job_id), head["version"] + 1, head["checkpoint"], head["snapshot"]))
                return assignment
        raise AssertionError("unreachable")

    def claim_build_assignment(self, assignment_id: UUID, worker_id: str) -> FactoryBuildAssignmentV2:
        if not worker_id:
            raise ValueError("worker id is required")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            assignment_row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id=?", (str(assignment_id),)).fetchone()
            if assignment_row is None:
                raise CreationNotFoundError(str(assignment_id))
            claim = db.execute("SELECT worker_id FROM creation_assignment_claims WHERE assignment_id=?", (str(assignment_id),)).fetchone()
            if claim is not None:
                if claim["worker_id"] != worker_id:
                    raise CreationConflictError("assignment is already claimed")
            else:
                db.execute("INSERT INTO creation_assignment_claims VALUES (?, ?, ?)", (str(assignment_id), worker_id, _json({"assignment_id": str(assignment_id), "worker_id": worker_id})))
                assignment = FactoryBuildAssignmentV2.model_validate_json(assignment_row["payload"])
                head = self._head(assignment.creation_job_id, db)
                if head["status"] == "awaiting_build":
                    db.execute("INSERT INTO creation_heads VALUES (?, ?, 'building', ?, ?)", (str(assignment.creation_job_id), head["version"] + 1, head["checkpoint"], head["snapshot"]))
        return FactoryBuildAssignmentV2.model_validate_json(assignment_row["payload"])

    def accept_build_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
        encoded = _json(effect)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id=?", (str(effect.assignment_id),)).fetchone()
            if row is None:
                raise CreationNotFoundError(str(effect.assignment_id))
            assignment = FactoryBuildAssignmentV2.model_validate_json(row["payload"])
            try:
                validate_build_effect_binding(assignment, effect)
            except ValueError as error:
                raise CreationConflictError(str(error)) from error
            existing = db.execute("SELECT payload FROM creation_build_effects WHERE assignment_id=?", (str(effect.assignment_id),)).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CreationConflictError("build effect already differs")
                return FactoryBuildEffectV2.model_validate_json(existing["payload"])
            db.execute("INSERT INTO creation_build_effects VALUES (?, ?, ?)", (str(effect.assignment_id), effect.effect_key, encoded))
            head = self._head(effect.creation_job_id, db)
            if head["status"] == "building":
                db.execute("INSERT INTO creation_heads VALUES (?, ?, 'validating', ?, ?)", (str(effect.creation_job_id), head["version"] + 1, head["checkpoint"], head["snapshot"]))
        return effect

    def build_effect(self, assignment_id: UUID) -> FactoryBuildEffectV2 | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM creation_build_effects WHERE assignment_id=?", (str(assignment_id),)).fetchone()
        return None if row is None else FactoryBuildEffectV2.model_validate_json(row["payload"])

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

    def finish(self, result: CreationResultV1 | CreationResultV2) -> CreationResultV1 | CreationResultV2:
        encoded = _json(result)
        job_id = str(result.creation_job_id)
        with self._connect() as db:
            existing = db.execute(
                "SELECT payload FROM creation_results WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CreationConflictError("creation result already differs")
                return self._parse_result(existing["payload"])
            head = self._head(result.creation_job_id, db)
            db.execute("INSERT INTO creation_results VALUES (?, ?)", (job_id, encoded))
            db.execute(
                "INSERT INTO creation_heads VALUES (?, ?, ?, ?, ?)",
                (job_id, head["version"] + 1, result.status, head["checkpoint"], head["snapshot"]),
            )
        return result

    @staticmethod
    def _parse_result(payload: str) -> CreationResultV1 | CreationResultV2:
        parsed = json.loads(payload)
        return (CreationResultV2 if parsed.get("schema") == "minibook.creation-result.v2" else CreationResultV1).model_validate(parsed)

    def result(self, job_id: UUID) -> CreationResultV1 | CreationResultV2 | None:
        self.job(job_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM creation_results WHERE job_id = ?", (str(job_id),)
            ).fetchone()
        return None if row is None else self._parse_result(row["payload"])
