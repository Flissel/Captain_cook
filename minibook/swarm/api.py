"""Optional HTTP boundary for persisted creation jobs."""
from __future__ import annotations

import secrets
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import CreationJobV1
from .job_store import CreationConflictError, CreationJobStore, CreationNotFoundError


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


def create_creation_router(
    store: CreationJobStore | None,
    *,
    api_key: str | None,
    schedule: Callable[[UUID], None] | None = None,
) -> APIRouter:
    expected = (api_key or "").strip()

    def require_creation_bearer(
        authorization: str | None = Header(default=None),
    ) -> None:
        if not expected:
            raise HTTPException(503, "Creation API authentication is not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(
                401, "Creation API bearer credential is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = authorization.removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, "Creation API bearer credential is invalid")

    router = APIRouter(dependencies=[Depends(require_creation_bearer)])

    @router.get("/api/v1/creation-capabilities")
    def capabilities() -> dict[str, object]:
        return {
            "schema": "minibook.creation-capabilities.v1",
            "creation_jobs": store is not None,
        }

    @router.post("/api/v1/creation-jobs")
    def submit(job: CreationJobV1):
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            receipt = store.submit(job)
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not receipt.replayed and schedule is not None:
            schedule(job.creation_job_id)
        return JSONResponse(
            receipt.model_dump(mode="json"), status_code=200 if receipt.replayed else 202
        )

    @router.get("/api/v1/creation-jobs/{job_id}")
    def status(job_id: UUID):
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            return store.progress(job_id).model_dump(mode="json", by_alias=True)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc

    @router.post("/api/v1/creation-jobs/{job_id}/cancel")
    def cancel(job_id: UUID, request: CancelRequest):
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

    @router.get("/api/v1/creation-jobs/{job_id}/result")
    def result(job_id: UUID):
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            value = store.result(job_id)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc
        if value is None:
            raise HTTPException(409, "Creation result is not available")
        return value.model_dump(mode="json", by_alias=True)

    return router
