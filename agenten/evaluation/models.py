"""Frozen contracts for deterministic evaluation source inventorying."""

from __future__ import annotations

import hashlib
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SECRET_ASSIGNMENT = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<name>(?:[A-Za-z][A-Za-z0-9_]*_)?(?:API_KEY|TOKEN)|password)=(?P<value>[^\r\n]*)$"
)


class EvaluationStatus(str, Enum):
    CREATED = "created"
    INVENTORYING = "inventorying"
    PLANNING = "planning"
    ACCEPTED = "accepted"
    PARTIAL = "partial"
    FAILED = "failed"


class SourceBlock(BaseModel):
    """One immutable, redacted Markdown fragment safe for later model use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(pattern=r"^block-[0-9]{4}$")
    heading_path: tuple[str, ...] = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def has_ordered_lines_and_matching_digest(self) -> "SourceBlock":
        if self.line_end < self.line_start:
            raise ValueError("line_end must not precede line_start")
        if any(not heading.strip() for heading in self.heading_path):
            raise ValueError("heading_path entries must be non-empty")
        if self.sha256 != hashlib.sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("sha256 does not match redacted block text")
        if any(match.group("value") != "[REDACTED]" for match in _SECRET_ASSIGNMENT.finditer(self.text)):
            raise ValueError("source block credential assignments must be redacted")
        return self


class EvaluationSource(BaseModel):
    """Redacted blocks and original-byte provenance, without a filesystem path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=1)
    blocks: tuple[SourceBlock, ...] = Field(min_length=1)

    @field_validator("source_reference")
    @classmethod
    def source_reference_is_logical_and_relative(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        segments = normalized.split("/")
        if (
            normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", normalized)
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ValueError("source_reference must be a safe logical relative path")
        return normalized


class EvaluationRun(BaseModel):
    """Frozen later-task execution envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    source: EvaluationSource
    status: EvaluationStatus
    max_rounds: int = Field(ge=1)
    max_calls: int = Field(ge=1)
