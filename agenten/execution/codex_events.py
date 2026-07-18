"""Secret-safe parsing for Codex CLI JSONL lifecycle output."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CodexLifecycle = Literal[
    "started",
    "turn_started",
    "turn_completed",
    "item_started",
    "item_updated",
    "item_completed",
    "failed",
]
CodexWarningType = Literal["malformed_json", "unknown_event", "invalid_event"]


class CodexProcessEvent(BaseModel):
    """Sanitized metadata for one recognized Codex process event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_type: Literal["codex_session"] = "codex_session"
    lifecycle: CodexLifecycle
    source_sequence: int | None = Field(default=None, ge=0)
    session_id: str | None = Field(default=None, min_length=1)
    item_id: str | None = Field(default=None, min_length=1)
    item_type: str | None = Field(default=None, min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class CodexParseWarning(BaseModel):
    """Content-free evidence that a Codex JSONL line could not be trusted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_type: Literal["codex_session_warning"] = "codex_session_warning"
    source_sequence: int | None = Field(default=None, ge=0)
    warning_type: CodexWarningType
    line_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def parse_codex_jsonl(line: str) -> CodexProcessEvent | CodexParseWarning:
    """Parse one JSONL record without retaining untrusted textual content."""
    line_sha256 = hashlib.sha256(
        line.encode("utf-8", "surrogatepass")
    ).hexdigest()
    try:
        record = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return _warning("malformed_json", line_sha256)

    if not isinstance(record, dict):
        return _warning("invalid_event", line_sha256)
    if _contains_lone_surrogate(record):
        return _warning("malformed_json", line_sha256)

    record_type = record.get("type")
    if not isinstance(record_type, str):
        return _warning("invalid_event", line_sha256)

    try:
        if record_type == "thread.started":
            thread_id = record.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                return _warning("invalid_event", line_sha256)
            return CodexProcessEvent(
                lifecycle="started",
                session_id=thread_id,
            )
        if record_type == "turn.started":
            return CodexProcessEvent(lifecycle="turn_started")
        if record_type == "turn.completed":
            usage = record.get("usage")
            if not isinstance(usage, dict):
                return _warning("invalid_event", line_sha256)
            return CodexProcessEvent(
                lifecycle="turn_completed",
                input_tokens=usage.get("input_tokens"),
                cached_input_tokens=usage.get("cached_input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        if record_type in {"item.started", "item.updated", "item.completed"}:
            item = record.get("item")
            if not isinstance(item, dict):
                return _warning("invalid_event", line_sha256)
            item_id = item.get("id")
            item_type = item.get("type")
            if (
                not isinstance(item_id, str)
                or not item_id
                or not isinstance(item_type, str)
                or not item_type
            ):
                return _warning("invalid_event", line_sha256)
            return CodexProcessEvent(
                lifecycle={
                    "item.started": "item_started",
                    "item.updated": "item_updated",
                    "item.completed": "item_completed",
                }[record_type],
                item_id=item_id,
                item_type=item_type,
            )
        if record_type == "error":
            if not isinstance(record.get("message"), str):
                return _warning("invalid_event", line_sha256)
            return CodexProcessEvent(lifecycle="failed")
    except ValidationError:
        return _warning("invalid_event", line_sha256)

    return _warning("unknown_event", line_sha256)


def _warning(
    warning_type: CodexWarningType, line_sha256: str
) -> CodexParseWarning:
    return CodexParseWarning(
        warning_type=warning_type,
        line_sha256=line_sha256,
    )


def _contains_lone_surrogate(value: object) -> bool:
    if isinstance(value, str):
        return any("\ud800" <= character <= "\udfff" for character in value)
    if isinstance(value, dict):
        return any(
            _contains_lone_surrogate(key) or _contains_lone_surrogate(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_lone_surrogate(item) for item in value)
    return False
