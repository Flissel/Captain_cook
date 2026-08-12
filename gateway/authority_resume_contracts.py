"""Frozen contracts for the authority resume flow.

Resume Authorize -> Dispatch -> Readback: the Gateway issues a single-use,
short-lived resume authorization for one assembly, consumes it exactly once
when the dispatch is recorded, and answers readback only from persisted,
redacted evidence. Raw authorization tokens exist only in the issue
response; persistence stores their SHA-256 digest.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = r"^[a-f0-9]{64}$"
MAX_AUTHORIZATION_TTL = timedelta(minutes=10)

AuthorityResumeDenialReason = Literal[
    "unknown_authorization",
    "already_consumed",
    "expired",
    "assembly_mismatch",
]


class AuthorityResumeError(ValueError):
    def __init__(self, reason: AuthorityResumeDenialReason) -> None:
        super().__init__(reason)
        self.reason: AuthorityResumeDenialReason = reason


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResumeAuthorizationV1(_FrozenContract):
    authorization_id: UUID
    assembly_id: str = Field(pattern=SHA256_PATTERN)
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    @model_validator(mode="after")
    def require_bounded_ttl(self) -> "ResumeAuthorizationV1":
        if not self.issued_at < self.expires_at <= (
            self.issued_at + MAX_AUTHORIZATION_TTL
        ):
            raise ValueError(
                "resume authorization expiry must be at most ten minutes"
            )
        return self


class DispatchRecordV1(_FrozenContract):
    dispatch_id: UUID
    assembly_id: str = Field(pattern=SHA256_PATTERN)
    authorization_id: UUID
    revision: int = Field(ge=1, strict=True)
    dispatched_at: datetime


class AuthorityReadbackV1(_FrozenContract):
    assembly_id: str = Field(pattern=SHA256_PATTERN)
    revision: int = Field(ge=0, strict=True)
    authorization_count: int = Field(ge=0, strict=True)
    dispatches: tuple[DispatchRecordV1, ...]
