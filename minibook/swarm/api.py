"""Optional HTTP boundary for persisted creation jobs."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import re
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifact_store import FilesystemCreationArtifactStore
from .contracts import CreationJobV1, CreationJobV2, CreationJobV3, FactoryBuildEffectV2
from .job_store import CreationConflictError, CreationJobStore, CreationNotFoundError


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _parse_job(payload: dict[str, object]) -> CreationJobV1 | CreationJobV2 | CreationJobV3:
    schemas = {
        "minibook.creation-job.v1": CreationJobV1,
        "minibook.creation-job.v2": CreationJobV2,
        "minibook.creation-job.v3": CreationJobV3,
    }
    schema = payload.get("schema", payload.get("schema_name"))
    model = schemas.get(schema) if isinstance(schema, str) else None
    if model is None:
        raise HTTPException(422, "Creation job schema is unsupported")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "Creation job payload is invalid") from exc


def create_creation_router(
    store: CreationJobStore | None,
    *,
    schedule: Callable[[UUID], None] | None = None,
    creation_api_key: str | None = None,
    worker_token: str | None = None,
    worker_id: str | None = None,
    artifact_store: FilesystemCreationArtifactStore | None = None,
) -> APIRouter:
    router = APIRouter()

    def require_store() -> CreationJobStore:
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        return store

    def require_caller(authorization: str | None) -> None:
        if creation_api_key is None:
            raise HTTPException(503, "Creation caller authentication is not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Creation caller authorization is required")
        supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, creation_api_key):
            raise HTTPException(403, "Creation caller authorization failed")

    def require_worker(authorization: str | None) -> str:
        if worker_token is None or worker_id is None:
            raise HTTPException(503, "V3 creation worker is not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Worker bearer authorization is required")
        supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, worker_token):
            raise HTTPException(403, "Worker authorization failed")
        return worker_id

    def require_v3_job(job_id: UUID) -> CreationJobV3:
        try:
            job = require_store().job(job_id)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc
        if not isinstance(job, CreationJobV3):
            raise HTTPException(422, "V3 creation endpoint requires a V3 job")
        return job

    @router.get("/api/v1/creation-capabilities")
    def capabilities() -> dict[str, object]:
        return {
            "schema": "minibook.creation-capabilities.v1",
            "creation_jobs": store is not None,
        }

    @router.post("/api/v1/creation-jobs")
    def submit(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ):
        require_caller(authorization)
        current_store = require_store()
        job = _parse_job(payload)
        if isinstance(job, CreationJobV3) and (worker_token is None or worker_id is None):
            raise HTTPException(503, "V3 creation worker is not configured")
        try:
            receipt = current_store.submit(job)
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        progress = current_store.progress(job.creation_job_id)
        if schedule is not None and progress.status in {
            "queued", "planning", "validating", "packaging", "readback", "running"
        }:
            schedule(job.creation_job_id)
        return JSONResponse(
            receipt.model_dump(mode="json"), status_code=200 if receipt.replayed else 202
        )

    @router.post("/v3/creation/jobs")
    def submit_v3(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ):
        require_caller(authorization)
        job = _parse_job(payload)
        if not isinstance(job, CreationJobV3):
            raise HTTPException(422, "V3 creation endpoint requires a V3 job")
        return submit(payload, authorization)

    @router.get("/api/v1/creation-jobs/{job_id}")
    def status(job_id: UUID, authorization: str | None = Header(default=None)):
        require_caller(authorization)
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            return store.progress(job_id).model_dump(mode="json", by_alias=True)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc

    @router.get("/v3/creation/jobs/{job_id}")
    def status_v3(job_id: UUID, authorization: str | None = Header(default=None)):
        require_caller(authorization)
        require_v3_job(job_id)
        return status(job_id, authorization)

    @router.post("/api/v1/creation-jobs/{job_id}/cancel")
    def cancel(
        job_id: UUID,
        request: CancelRequest,
        authorization: str | None = Header(default=None),
    ):
        require_caller(authorization)
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            return store.cancel(job_id, request.expected_version).model_dump(
                mode="json", by_alias=True
            )
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/v3/creation/jobs/{job_id}/cancel")
    def cancel_v3(
        job_id: UUID,
        request: CancelRequest,
        authorization: str | None = Header(default=None),
    ):
        require_caller(authorization)
        require_v3_job(job_id)
        return cancel(job_id, request, authorization)

    @router.get("/api/v1/creation-jobs/{job_id}/result")
    def result(job_id: UUID, authorization: str | None = Header(default=None)):
        require_caller(authorization)
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            value = store.result(job_id)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc
        if value is None:
            raise HTTPException(409, "Creation result is not available")
        return value.model_dump(mode="json", by_alias=True)

    @router.get("/v3/creation/jobs/{job_id}/result")
    def result_v3(job_id: UUID, authorization: str | None = Header(default=None)):
        require_caller(authorization)
        require_v3_job(job_id)
        return result(job_id, authorization)

    @router.get("/v3/creation/assignments/next")
    def next_assignment(authorization: str | None = Header(default=None)):
        bound_worker = require_worker(authorization)
        current_store = require_store()
        assignment = current_store.next_claimable_build_assignment(bound_worker)
        if assignment is None:
            return Response(status_code=204)
        return assignment.model_dump(mode="json", by_alias=True)

    @router.post("/v3/creation/assignments/{assignment_id}/claim")
    def claim_assignment(
        assignment_id: UUID,
        request: ClaimRequest,
        authorization: str | None = Header(default=None),
    ):
        bound_worker = require_worker(authorization)
        if request.worker_id != bound_worker:
            raise HTTPException(403, "Worker identity does not match bearer authorization")
        try:
            assignment = require_store().claim_build_assignment(assignment_id, bound_worker)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation assignment not found") from exc
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        return assignment.model_dump(mode="json", by_alias=True)

    @router.post("/v3/creation/assignments/{assignment_id}/effect")
    def submit_effect(
        assignment_id: UUID,
        effect: FactoryBuildEffectV2,
        authorization: str | None = Header(default=None),
    ):
        bound_worker = require_worker(authorization)
        if effect.assignment_id != assignment_id:
            raise HTTPException(409, "Build effect assignment identity changed")
        current_store = require_store()
        with current_store._connect() as db:
            claim = db.execute(
                "SELECT worker_id FROM creation_assignment_claims WHERE assignment_id = ?",
                (str(assignment_id),),
            ).fetchone()
        if claim is not None and claim["worker_id"] != bound_worker:
            raise HTTPException(403, "Build effect worker does not own the assignment claim")
        try:
            accepted = current_store.accept_build_effect(effect)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation assignment not found") from exc
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        acknowledgement = current_store.progress(accepted.creation_job_id)
        if schedule is not None:
            schedule(accepted.creation_job_id)
        return JSONResponse(
            acknowledgement.model_dump(mode="json", by_alias=True), status_code=202
        )

    @router.get("/v3/creation/artifacts/{sha256}/metadata")
    def artifact_metadata(
        sha256: str,
        authorization: str | None = Header(default=None),
    ):
        require_worker(authorization)
        if artifact_store is None:
            raise HTTPException(503, "V3 creation artifact store is not configured")
        if re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise HTTPException(422, "Artifact digest is invalid")
        target = artifact_store.root / "objects" / sha256[:2] / sha256
        if not target.is_file():
            raise HTTPException(404, "Creation artifact not found")
        content = target.read_bytes()
        if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), sha256):
            raise HTTPException(409, "Creation artifact digest changed")
        return {"sha256": sha256, "size_bytes": len(content)}

    return router
