"""Optional HTTP boundary for persisted creation jobs."""
from __future__ import annotations

import secrets
from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    CreationCompletionEvidenceV1,
    CreationEvidenceReceiptV1,
    CreationJobV1,
    CreationPreparationEvidenceV1,
    FactoryEvidenceBlockV1,
)
from .job_store import CreationConflictError, CreationJobStore, CreationNotFoundError


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


def create_creation_router(
    store: CreationJobStore | None,
    *,
    schedule: Callable[[UUID], None] | None = None,
    api_key: str | None = None,
) -> APIRouter:
    def authorize(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> None:
        if store is None:
            return
        if not api_key:
            raise HTTPException(503, "Creation API authentication is not configured")
        supplied = authorization or ""
        if not secrets.compare_digest(supplied, f"Bearer {api_key}"):
            raise HTTPException(401, "Invalid or missing creation API key")

    router = APIRouter(dependencies=[Depends(authorize)])

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

    @router.put("/api/v1/creation-jobs/{job_id}/preparation-evidence")
    def record_preparation(
        job_id: UUID,
        evidence: CreationPreparationEvidenceV1,
    ):
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        if evidence.creation_job.creation_job_id != job_id:
            raise HTTPException(409, "Preparation evidence path identity changed")
        try:
            replayed = store.record_preparation(evidence)
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        receipt = CreationEvidenceReceiptV1(
            creation_job_id=job_id,
            replayed=replayed,
        )
        return JSONResponse(
            receipt.model_dump(mode="json"),
            status_code=200 if replayed else 201,
        )

    @router.get(
        "/api/v1/creation-jobs/{job_id}/preparation-blocks",
        response_model=tuple[FactoryEvidenceBlockV1, FactoryEvidenceBlockV1],
    )
    def preparation_blocks(
        job_id: UUID,
    ) -> tuple[FactoryEvidenceBlockV1, FactoryEvidenceBlockV1]:
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            return store.preparation(job_id).blocks
        except CreationNotFoundError as exc:
            _raise_pending_or_missing(
                store,
                job_id,
                "Creation preparation evidence is not available",
                exc,
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
            _raise_pending_or_missing(
                store,
                job_id,
                "Creation result is not available",
                None,
            )
        return value.model_dump(mode="json", by_alias=True)

    @router.put("/api/v1/creation-jobs/{job_id}/completion-evidence")
    def record_completion(
        job_id: UUID,
        evidence: CreationCompletionEvidenceV1,
    ):
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        if evidence.result.creation_job_id != job_id:
            raise HTTPException(409, "Completion evidence path identity changed")
        try:
            replayed = store.record_completion(evidence)
        except CreationNotFoundError as exc:
            raise HTTPException(404, "Creation job not found") from exc
        except CreationConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        receipt = CreationEvidenceReceiptV1(
            creation_job_id=job_id,
            replayed=replayed,
        )
        return JSONResponse(
            receipt.model_dump(mode="json"),
            status_code=200 if replayed else 201,
        )

    @router.get(
        "/api/v1/creation-jobs/{job_id}/completion-block",
        response_model=FactoryEvidenceBlockV1,
    )
    def completion_block(job_id: UUID) -> FactoryEvidenceBlockV1:
        if store is None:
            raise HTTPException(503, "Creation jobs are not configured")
        try:
            return store.completion(job_id).block
        except CreationNotFoundError as exc:
            _raise_pending_or_missing(
                store,
                job_id,
                "Creation completion evidence is not available",
                exc,
            )

    return router


def _raise_pending_or_missing(
    store: CreationJobStore,
    job_id: UUID,
    detail: str,
    cause: CreationNotFoundError | None,
) -> None:
    try:
        progress = store.progress(job_id)
    except CreationNotFoundError:
        raise HTTPException(404, "Creation job not found") from cause
    if progress.status in {"succeeded", "failed", "blocked", "cancelled"}:
        raise HTTPException(
            422,
            f"{detail}; creation status is {progress.status}",
        ) from cause
    raise HTTPException(409, detail, headers={"Retry-After": "1"}) from cause
