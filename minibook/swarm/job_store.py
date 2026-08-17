"""Append-only SQLite persistence for resumable creation jobs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from .contracts import (
    CreationJobV1,
    CreationJobV2,
    CreationJobV3,
    CreationProgressV1,
    CreationResultV1,
    CreationResultV2,
    CreationSubmissionReceipt,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    validate_action_response_binding,
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
    _TERMINAL = frozenset({"succeeded", "failed", "blocked", "cancelled"})
    _V3_TRANSITIONS = {
        "queued": frozenset({"planning", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "planning": frozenset({"planning", "awaiting_build", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "awaiting_build": frozenset({"building", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "building": frozenset({"validating", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "validating": frozenset({"validating", "packaging", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "packaging": frozenset({"packaging", "readback", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "readback": frozenset({"readback", "succeeded", "awaiting_user_input", "blocked", "failed", "cancelled"}),
        "awaiting_user_input": frozenset({"blocked", "failed", "cancelled"}),
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

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
                    request_payload TEXT NOT NULL, response_payload TEXT,
                    action_status TEXT NOT NULL, prior_status TEXT NOT NULL,
                    prior_checkpoint TEXT, prior_snapshot TEXT NOT NULL
                );
                """
            )

    def submit(self, job: CreationJobV1 | CreationJobV2 | CreationJobV3) -> CreationSubmissionReceipt:
        job_id = str(job.creation_job_id)
        payload = _json(job)
        initial_snapshot = _json({"creation_job_id": job_id})
        with self._immediate_transaction() as db:
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

    @staticmethod
    def _parse_job(payload: str) -> CreationJobV1 | CreationJobV2 | CreationJobV3:
        parsed = json.loads(payload)
        parsers = {
            "minibook.creation-job.v1": CreationJobV1,
            "minibook.creation-job.v2": CreationJobV2,
            "minibook.creation-job.v3": CreationJobV3,
        }
        try:
            return parsers[parsed.get("schema")].model_validate(parsed)
        except KeyError as error:
            raise CreationConflictError("unknown persisted creation job schema") from error

    def _job(self, job_id: UUID, db: sqlite3.Connection) -> CreationJobV1 | CreationJobV2 | CreationJobV3:
        row = db.execute("SELECT payload FROM creation_jobs WHERE job_id = ?", (str(job_id),)).fetchone()
        if row is None:
            raise CreationNotFoundError(str(job_id))
        return self._parse_job(row["payload"])

    def job(self, job_id: UUID) -> CreationJobV1 | CreationJobV2 | CreationJobV3:
        with self._connect() as db:
            return self._job(job_id, db)

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

    @staticmethod
    def _require_version(head: sqlite3.Row, expected_version: int) -> None:
        if head["version"] != expected_version:
            raise CreationConflictError("creation job version changed")

    def _require_transition(
        self,
        job: CreationJobV1 | CreationJobV2 | CreationJobV3,
        current: str,
        target: str,
    ) -> None:
        if current in self._TERMINAL:
            raise CreationConflictError("creation job is already terminal")
        if isinstance(job, CreationJobV3):
            if target not in self._V3_TRANSITIONS.get(current, frozenset()):
                raise CreationConflictError(f"illegal V3 creation transition: {current} -> {target}")
            return
        if target not in {"running", "blocked", "failed", "cancelled", "succeeded"}:
            raise CreationConflictError(f"illegal legacy creation transition: {current} -> {target}")

    @staticmethod
    def _append_head(
        db: sqlite3.Connection,
        *,
        job_id: UUID,
        head: sqlite3.Row,
        status: str,
        checkpoint: str | None = None,
        snapshot: str | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO creation_heads VALUES (?, ?, ?, ?, ?)",
            (
                str(job_id), head["version"] + 1, status,
                head["checkpoint"] if checkpoint is None else checkpoint,
                head["snapshot"] if snapshot is None else snapshot,
            ),
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
                WHERE heads.status IN ('queued', 'planning', 'validating', 'packaging', 'readback', 'running')
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
        self, job_id: UUID, effect_key: str, receipt: dict[str, Any],
        *, expected_version: int,
    ) -> dict[str, Any]:
        encoded = _json(receipt)
        with self._immediate_transaction() as db:
            row = db.execute(
                "SELECT receipt FROM creation_effect_receipts WHERE job_id = ? AND effect_key = ?",
                (str(job_id), effect_key),
            ).fetchone()
            if row is not None:
                if row["receipt"] != encoded:
                    raise CreationConflictError("effect key already has different receipt")
                return json.loads(row["receipt"])
            head = self._head(job_id, db)
            self._require_version(head, expected_version)
            if head["status"] in self._TERMINAL:
                raise CreationConflictError("creation job is already terminal")
            db.execute(
                "INSERT INTO creation_effect_receipts VALUES (?, ?, ?)",
                (str(job_id), effect_key, encoded),
            )
        return receipt

    def complete_step(
        self,
        job_id: UUID,
        step: str,
        effect_key: str,
        snapshot: dict[str, Any],
        *,
        expected_version: int,
    ) -> None:
        encoded = _json(snapshot)
        with self._immediate_transaction() as db:
            existing = db.execute(
                "SELECT snapshot FROM creation_step_receipts WHERE job_id = ? AND step = ?",
                (str(job_id), step),
            ).fetchone()
            if existing is not None:
                if existing["snapshot"] != encoded:
                    raise CreationConflictError("step already has different receipt")
                return
            head = self._head(job_id, db)
            self._require_version(head, expected_version)
            job = self._job(job_id, db)
            target = "planning" if isinstance(job, CreationJobV3) and head["status"] == "queued" else head["status"] if isinstance(job, CreationJobV3) else "running"
            self._require_transition(job, head["status"], target)
            db.execute(
                "INSERT INTO creation_step_receipts VALUES (?, ?, ?, ?)",
                (str(job_id), step, effect_key, encoded),
            )
            self._append_head(db, job_id=job_id, head=head, status=target, checkpoint=step, snapshot=encoded)

    def transition(self, job_id: UUID, status: str, *, expected_version: int, checkpoint: str | None = None, snapshot: dict[str, Any] | None = None) -> CreationProgressV1:
        """Append a version-fenced state transition without overwriting evidence."""
        with self._immediate_transaction() as db:
            head = self._head(job_id, db)
            self._require_version(head, expected_version)
            job = self._job(job_id, db)
            self._require_transition(job, head["status"], status)
            self._append_head(db, job_id=job_id, head=head, status=status, checkpoint=checkpoint, snapshot=None if snapshot is None else _json(snapshot))
        return self.progress(job_id)

    def publish_build_assignment(self, assignment: FactoryBuildAssignmentV2) -> FactoryBuildAssignmentV2:
        encoded = _json(assignment)
        with self._immediate_transaction() as db:
            row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id = ?", (str(assignment.assignment_id),)).fetchone()
            if row is not None:
                if row["payload"] != encoded:
                    raise CreationConflictError("assignment id already has different content")
                return FactoryBuildAssignmentV2.model_validate_json(row["payload"])
            try:
                job = self._job(assignment.creation_job_id, db)
                head = self._head(assignment.creation_job_id, db)
            except CreationNotFoundError as error:
                raise CreationConflictError("assignment does not match a stored V3 job") from error
            if not isinstance(job, CreationJobV3):
                raise CreationConflictError("assignment does not match a stored V3 job")
            bindings = (
                assignment.factory_job_id == job.factory_job_id,
                assignment.correlation_id == job.correlation_id,
                assignment.subject_version == job.subject_version,
                assignment.attempt == job.attempt,
                assignment.workspace_policy_sha256 == job.workspace_policy_ref.sha256,
                assignment.lease_ref == job.lease_ref,
                assignment.required_agent_ids == job.required_agent_ids,
                assignment.ordered_handoffs == job.ordered_handoffs,
                assignment.tool_requirements == job.tool_requirements,
                assignment.deadline_at == job.deadline_at,
            )
            if not all(bindings):
                raise CreationConflictError("assignment bindings do not match stored V3 job")
            if head["version"] != assignment.checkpoint_version or head["status"] != "planning":
                raise CreationConflictError("assignment checkpoint is no longer current")
            conflicting = db.execute("SELECT payload FROM creation_build_assignments WHERE job_id=? AND checkpoint_version=? AND effect_key=?", (str(assignment.creation_job_id), assignment.checkpoint_version, assignment.effect_key)).fetchone()
            if conflicting is not None and conflicting["payload"] != encoded:
                raise CreationConflictError("assignment identity already has different content")
            db.execute("INSERT INTO creation_build_assignments VALUES (?, ?, ?, ?, ?)", (str(assignment.assignment_id), str(assignment.creation_job_id), assignment.checkpoint_version, assignment.effect_key, encoded))
            self._require_transition(job, head["status"], "awaiting_build")
            self._append_head(db, job_id=job.creation_job_id, head=head, status="awaiting_build")
        return assignment

    def claim_build_assignment(self, assignment_id: UUID, worker_id: str) -> FactoryBuildAssignmentV2:
        if not worker_id:
            raise ValueError("worker id is required")
        with self._immediate_transaction() as db:
            assignment_row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id=?", (str(assignment_id),)).fetchone()
            if assignment_row is None:
                raise CreationNotFoundError(str(assignment_id))
            claim = db.execute("SELECT worker_id FROM creation_assignment_claims WHERE assignment_id=?", (str(assignment_id),)).fetchone()
            if claim is not None:
                if claim["worker_id"] != worker_id:
                    raise CreationConflictError("assignment is already claimed by another worker")
            else:
                assignment = FactoryBuildAssignmentV2.model_validate_json(assignment_row["payload"])
                head = self._head(assignment.creation_job_id, db)
                job = self._job(assignment.creation_job_id, db)
                if datetime.now(timezone.utc) >= assignment.deadline_at:
                    raise CreationConflictError("assignment deadline expired")
                # A durable user-action pause/resume advances the head version
                # without replacing the immutable assignment.  The restored
                # status remains the lifecycle authority for claimability.
                if head["status"] != "awaiting_build" or head["version"] < assignment.checkpoint_version + 1:
                    raise CreationConflictError("assignment is not available for claim")
                db.execute("INSERT INTO creation_assignment_claims VALUES (?, ?, ?)", (str(assignment_id), worker_id, _json({"assignment_id": str(assignment_id), "worker_id": worker_id})))
                self._require_transition(job, head["status"], "building")
                self._append_head(db, job_id=assignment.creation_job_id, head=head, status="building")
        return FactoryBuildAssignmentV2.model_validate_json(assignment_row["payload"])

    def accept_build_effect(self, effect: FactoryBuildEffectV2) -> FactoryBuildEffectV2:
        encoded = _json(effect)
        with self._immediate_transaction() as db:
            row = db.execute("SELECT payload FROM creation_build_assignments WHERE assignment_id=?", (str(effect.assignment_id),)).fetchone()
            if row is None:
                raise CreationNotFoundError(str(effect.assignment_id))
            assignment = FactoryBuildAssignmentV2.model_validate_json(row["payload"])
            try:
                validate_build_effect_binding(assignment, effect)
            except ValueError as error:
                raise CreationConflictError(str(error)) from error
            claim = db.execute("SELECT worker_id FROM creation_assignment_claims WHERE assignment_id=?", (str(effect.assignment_id),)).fetchone()
            if claim is None:
                raise CreationConflictError("assignment must be claimed before accepting an effect")
            existing = db.execute("SELECT payload FROM creation_build_effects WHERE assignment_id=?", (str(effect.assignment_id),)).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CreationConflictError("build effect already differs")
                return FactoryBuildEffectV2.model_validate_json(existing["payload"])
            head = self._head(effect.creation_job_id, db)
            job = self._job(effect.creation_job_id, db)
            if head["status"] in self._TERMINAL:
                raise CreationConflictError("creation job is already terminal")
            if datetime.now(timezone.utc) >= assignment.deadline_at:
                raise CreationConflictError("assignment deadline expired")
            # Action Inbox round-trips may advance the version while restoring
            # the same claimed build state and assignment identity.
            if head["status"] != "building" or head["version"] < assignment.checkpoint_version + 2:
                raise CreationConflictError("assignment effect is not valid for the current building state")
            db.execute("INSERT INTO creation_build_effects VALUES (?, ?, ?)", (str(effect.assignment_id), effect.effect_key, encoded))
            self._require_transition(job, head["status"], "validating")
            self._append_head(db, job_id=effect.creation_job_id, head=head, status="validating")
        return effect

    def build_effect(self, assignment_id: UUID) -> FactoryBuildEffectV2 | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM creation_build_effects WHERE assignment_id=?", (str(assignment_id),)).fetchone()
        return None if row is None else FactoryBuildEffectV2.model_validate_json(row["payload"])

    def build_assignment_for_job(
        self, job_id: UUID
    ) -> FactoryBuildAssignmentV2 | None:
        """Return the immutable assignment published for a V3 creation job."""

        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM creation_build_assignments WHERE job_id=? ORDER BY rowid",
                (str(job_id),),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise CreationConflictError("creation job has multiple build assignments")
        return FactoryBuildAssignmentV2.model_validate_json(rows[0]["payload"])

    def next_claimable_build_assignment(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> FactoryBuildAssignmentV2 | None:
        """Return only a currently claimable assignment or this worker's live replay.

        The subsequent claim transaction remains the final race authority.
        """

        if not worker_id:
            raise ValueError("worker id is required")
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ValueError("assignment query time must be timezone aware")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT assignments.payload AS assignment_payload,
                       jobs.payload AS job_payload,
                       claims.worker_id AS claimed_worker_id,
                       heads.status AS head_status,
                       heads.version AS head_version
                FROM creation_build_assignments AS assignments
                JOIN creation_jobs AS jobs ON jobs.job_id = assignments.job_id
                JOIN creation_heads AS heads
                  ON heads.job_id = assignments.job_id
                 AND heads.version = (
                    SELECT MAX(current.version)
                    FROM creation_heads AS current
                    WHERE current.job_id = assignments.job_id
                 )
                LEFT JOIN creation_assignment_claims AS claims
                  ON claims.assignment_id = assignments.assignment_id
                LEFT JOIN creation_build_effects AS effects
                  ON effects.assignment_id = assignments.assignment_id
                WHERE effects.assignment_id IS NULL
                  AND (claims.worker_id IS NULL OR claims.worker_id = ?)
                ORDER BY assignments.rowid
                """,
                (worker_id,),
            ).fetchall()
        for row in rows:
            job = self._parse_job(row["job_payload"])
            if not isinstance(job, CreationJobV3):
                continue
            assignment = FactoryBuildAssignmentV2.model_validate_json(
                row["assignment_payload"]
            )
            if current_time >= assignment.deadline_at:
                continue
            claimed_worker = row["claimed_worker_id"]
            if claimed_worker is None:
                is_current = (
                    row["head_status"] == "awaiting_build"
                    and row["head_version"] >= assignment.checkpoint_version + 1
                )
            else:
                is_current = (
                    claimed_worker == worker_id
                    and row["head_status"] == "building"
                    and row["head_version"] >= assignment.checkpoint_version + 2
                )
            if is_current:
                return assignment
        return None

    def action_request(self, request_id: UUID) -> FactoryActionRequestV1:
        with self._connect() as db:
            row = db.execute(
                "SELECT request_payload FROM creation_action_projections WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        if row is None:
            raise CreationNotFoundError(str(request_id))
        return FactoryActionRequestV1.model_validate_json(row["request_payload"])

    def action_response(self, request_id: UUID) -> FactoryActionResponseV1 | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT response_payload FROM creation_action_projections WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        if row is None:
            raise CreationNotFoundError(str(request_id))
        return (
            None
            if row["response_payload"] is None
            else FactoryActionResponseV1.model_validate_json(row["response_payload"])
        )

    def record_action_request(
        self, request: FactoryActionRequestV1, *, expected_version: int
    ) -> CreationProgressV1:
        encoded = _json(request)
        with self._immediate_transaction() as db:
            existing = db.execute(
                "SELECT * FROM creation_action_projections WHERE request_id=?",
                (str(request.request_id),),
            ).fetchone()
            if existing is not None:
                if existing["request_payload"] != encoded:
                    raise CreationConflictError("action request already has different content")
                return self.progress(request.creation_job_id)
            job = self._job(request.creation_job_id, db)
            head = self._head(request.creation_job_id, db)
            self._require_version(head, expected_version)
            if not isinstance(job, CreationJobV3) or (
                request.factory_job_id != job.factory_job_id
                or request.correlation_id != job.correlation_id
                or request.expected_subject_version != job.subject_version
                or request.status != "pending"
            ):
                raise CreationConflictError("action request does not match stored V3 job")
            self._require_transition(job, head["status"], "awaiting_user_input")
            db.execute(
                "INSERT INTO creation_action_projections VALUES (?, ?, ?, NULL, 'pending', ?, ?, ?)",
                (
                    str(request.request_id), str(request.creation_job_id), encoded,
                    head["status"], head["checkpoint"], head["snapshot"],
                ),
            )
            self._append_head(
                db, job_id=request.creation_job_id, head=head,
                status="awaiting_user_input",
            )
        return self.progress(request.creation_job_id)

    def accept_action_response(
        self, response: FactoryActionResponseV1, *, expected_version: int
    ) -> CreationProgressV1:
        encoded = _json(response)
        with self._immediate_transaction() as db:
            row = db.execute(
                "SELECT * FROM creation_action_projections WHERE request_id=?",
                (str(response.request_id),),
            ).fetchone()
            if row is None:
                raise CreationNotFoundError(str(response.request_id))
            if row["action_status"] != "pending":
                raise CreationConflictError("action request is already consumed or expired")
            request = FactoryActionRequestV1.model_validate_json(row["request_payload"])
            try:
                validate_action_response_binding(request, response)
            except ValueError as error:
                raise CreationConflictError(str(error)) from error
            if response.answered_at > request.deadline_at or datetime.now(timezone.utc) >= request.deadline_at:
                raise CreationConflictError("action request deadline expired")
            head = self._head(request.creation_job_id, db)
            self._require_version(head, expected_version)
            if head["status"] != "awaiting_user_input":
                raise CreationConflictError("creation job is not awaiting user input")
            job = self._job(request.creation_job_id, db)
            prior_status = row["prior_status"]
            if not isinstance(job, CreationJobV3) or prior_status not in self._V3_TRANSITIONS:
                raise CreationConflictError("action request has no resumable checkpoint")
            db.execute(
                "UPDATE creation_action_projections SET response_payload=?, action_status='consumed' WHERE request_id=?",
                (encoded, str(response.request_id)),
            )
            self._append_head(
                db, job_id=request.creation_job_id, head=head, status=prior_status,
                checkpoint=row["prior_checkpoint"], snapshot=row["prior_snapshot"],
            )
        return self.progress(request.creation_job_id)

    def expire_action_request(
        self, request_id: UUID, *, expected_version: int, now: datetime
    ) -> CreationProgressV1:
        if now.tzinfo is None:
            raise ValueError("expiry time must be timezone-aware")
        with self._immediate_transaction() as db:
            row = db.execute(
                "SELECT * FROM creation_action_projections WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
            if row is None:
                raise CreationNotFoundError(str(request_id))
            if row["action_status"] != "pending":
                raise CreationConflictError("action request is already consumed or expired")
            request = FactoryActionRequestV1.model_validate_json(row["request_payload"])
            if now < request.deadline_at:
                raise CreationConflictError("action request deadline has not expired")
            head = self._head(request.creation_job_id, db)
            self._require_version(head, expected_version)
            job = self._job(request.creation_job_id, db)
            self._require_transition(job, head["status"], "blocked")
            db.execute(
                "UPDATE creation_action_projections SET action_status='expired' WHERE request_id=?",
                (str(request_id),),
            )
            self._append_head(db, job_id=request.creation_job_id, head=head, status="blocked")
        return self.progress(request.creation_job_id)

    def cancel(self, job_id: UUID, expected_version: int) -> CreationProgressV1:
        with self._immediate_transaction() as db:
            head = self._head(job_id, db)
            self._require_version(head, expected_version)
            job = self._job(job_id, db)
            self._require_transition(job, head["status"], "cancelled")
            self._append_head(db, job_id=job_id, head=head, status="cancelled")
        return self.progress(job_id)

    def finish(self, result: CreationResultV1 | CreationResultV2, *, expected_version: int) -> CreationResultV1 | CreationResultV2:
        encoded = _json(result)
        job_id = str(result.creation_job_id)
        with self._immediate_transaction() as db:
            try:
                job = self._job(result.creation_job_id, db)
                head = self._head(result.creation_job_id, db)
            except CreationNotFoundError as error:
                raise CreationConflictError("creation result does not match a stored job") from error
            if isinstance(job, CreationJobV3) != isinstance(result, CreationResultV2):
                raise CreationConflictError("creation result schema does not match job schema")
            common_bindings = (
                result.creation_job_id == job.creation_job_id,
                result.correlation_id == job.correlation_id,
                result.subject_version == job.subject_version,
                result.attempt == job.attempt,
            )
            v2_binding = not isinstance(result, CreationResultV2) or (
                isinstance(job, CreationJobV3)
                and result.factory_job_id == job.factory_job_id
            )
            if not all(common_bindings) or not v2_binding:
                raise CreationConflictError("creation result bindings do not match stored job")
            existing = db.execute(
                "SELECT payload FROM creation_results WHERE job_id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload"] != encoded:
                    raise CreationConflictError("creation result already differs")
                return self._parse_result(existing["payload"])
            self._require_version(head, expected_version)
            already_terminal = head["status"] in self._TERMINAL
            if already_terminal and head["status"] != result.status:
                raise CreationConflictError("creation result conflicts with terminal job state")
            if not already_terminal:
                self._require_transition(job, head["status"], result.status)
            db.execute("INSERT INTO creation_results VALUES (?, ?)", (job_id, encoded))
            if not already_terminal:
                self._append_head(db, job_id=result.creation_job_id, head=head, status=result.status)
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
