"""Fail-closed Gateway routes for Resume Authorize -> Dispatch -> Readback."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.authority_adapter_bundle import (
    AuthorityAdapterBundleError,
    load_adapter_bundle,
)
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

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "config" / "authority-adapter-bundle.v1.json"


def _verify_pinned_authority_bundle() -> None:
    """Raise AuthorityAdapterBundleError unless every adapter matches its digest.

    Resuming means handing authority back to code. If that code has drifted
    from the bundle it was pinned as, the only safe answer is no. This raises
    the domain error; the route converts it to a denial that names no role and
    echoes no content.
    """
    load_adapter_bundle(BUNDLE_PATH, root=REPO_ROOT)


class _DispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)


def build_authority_resume_router(
    store_provider: Callable[[], AuthorityResumeStore],
    *,
    clock: Callable[[], datetime] | None = None,
    verify_authority_bundle: Callable[[], None] | None = None,
) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(timezone.utc))
    verify_bundle = verify_authority_bundle or _verify_pinned_authority_bundle

    @router.post(
        "/v1/authority/assemblies/{assembly_id}/resume-authorizations",
        status_code=status.HTTP_201_CREATED,
    )
    def authorize(
        assembly_id: str,
        _: GatewayRole = Depends(require_captain),
    ) -> dict[str, str]:
        _require_assembly_id(assembly_id)
        try:
            verify_bundle()
        except AuthorityAdapterBundleError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="authority_bundle_unverified",
            ) from None
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
