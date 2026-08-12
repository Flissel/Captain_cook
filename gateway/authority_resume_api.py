"""Fail-closed Gateway routes for Resume Authorize -> Dispatch -> Readback."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from gateway.auth import GatewayRole, require_captain
from gateway.authority_resume_contracts import (
    SHA256_PATTERN,
    AuthorityReadbackV1,
    AuthorityResumeError,
)
from gateway.authority_resume_store import AuthorityResumeStore

_DENIAL_STATUS = {
    "unknown_authorization": status.HTTP_403_FORBIDDEN,
    "assembly_mismatch": status.HTTP_403_FORBIDDEN,
    "expired": status.HTTP_403_FORBIDDEN,
    "already_consumed": status.HTTP_409_CONFLICT,
}


class _DispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)


def build_authority_resume_router(
    store_provider: Callable[[], AuthorityResumeStore],
    *,
    clock: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(timezone.utc))

    @router.post(
        "/v1/authority/assemblies/{assembly_id}/resume-authorizations",
        status_code=status.HTTP_201_CREATED,
    )
    def authorize(
        assembly_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, str]:
        _require_assembly_id(assembly_id)
        record, raw_token = store_provider().authorize(assembly_id, now=now())
        return {
            "authorization_id": str(record.authorization_id),
            "token": raw_token,
            "expires_at": record.expires_at.isoformat(),
        }

    @router.post(
        "/v1/authority/assemblies/{assembly_id}/dispatches",
        status_code=status.HTTP_201_CREATED,
    )
    def dispatch(
        assembly_id: str,
        body: _DispatchBody,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, object]:
        _require_assembly_id(assembly_id)
        try:
            record = store_provider().dispatch(assembly_id, body.token, now=now())
        except AuthorityResumeError as error:
            raise HTTPException(
                status_code=_DENIAL_STATUS[error.reason],
                detail=error.reason,
            ) from None
        return {
            "dispatch_id": str(record.dispatch_id),
            "revision": record.revision,
            "dispatched_at": record.dispatched_at.isoformat(),
        }

    @router.get("/v1/authority/assemblies/{assembly_id}/readback")
    def readback(
        assembly_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> AuthorityReadbackV1:
        _require_assembly_id(assembly_id)
        evidence = store_provider().readback(assembly_id)
        if evidence is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown assembly",
            )
        return evidence

    return router


def _require_assembly_id(assembly_id: str) -> None:
    import re

    if not re.fullmatch(SHA256_PATTERN, assembly_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="unknown assembly",
        )
