"""Secret-safe parsing of Claude Code `--output-format stream-json` output.

Only types, ids, counters and hashes survive; message text, tool inputs and
tool outputs are never retained. Claude's own cost figure is ignored because
the provider proxy settlement is authoritative.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Lifecycle = Literal["started", "tool_call", "tool_result", "turn_completed", "completed", "failed"]


class ClaudeStreamTooLargeError(ValueError):
    """The stream exceeds the journal size ceiling."""


class AgentEventV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal["agent_session"] = "agent_session"
    lifecycle: Lifecycle
    session_id: str | None = None
    item_id: str | None = None
    item_type: str | None = None
    is_error: bool | None = None
    result_subtype: str | None = None
    num_turns: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ClaudeStreamWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_type: Literal["malformed_json", "unknown_event", "invalid_event"]
    line_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClaudeRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str | None
    result_subtype: str | None
    num_turns: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    tool_error_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _content(record: dict[str, object]) -> list[object] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return []
    return content if isinstance(content, list) else None


def parse_claude_stream_line(line: str) -> tuple[AgentEventV2 | ClaudeStreamWarning, ...]:
    digest = hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()

    def warn(kind: Literal["malformed_json", "unknown_event", "invalid_event"]) -> tuple[ClaudeStreamWarning]:
        return (ClaudeStreamWarning(warning_type=kind, line_sha256=digest),)

    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return warn("malformed_json")
    if not isinstance(record, dict):
        return warn("invalid_event")
    kind = record.get("type")
    session = _text(record.get("session_id"))

    if kind == "system":
        if record.get("subtype") == "init":
            return (AgentEventV2(lifecycle="started", session_id=session),)
        return ()
    if kind == "assistant":
        content = _content(record)
        if content is None:
            return warn("invalid_event")
        calls = tuple(
            AgentEventV2(
                lifecycle="tool_call",
                session_id=session,
                item_id=_text(block.get("id")),
                item_type=_text(block.get("name")),
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
        return calls or (AgentEventV2(lifecycle="turn_completed", session_id=session),)
    if kind == "user":
        content = _content(record)
        if content is None:
            return warn("invalid_event")
        return tuple(
            AgentEventV2(
                lifecycle="tool_result",
                session_id=session,
                item_id=_text(block.get("tool_use_id")),
                is_error=bool(block.get("is_error", False)),
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    if kind == "result":
        subtype = _text(record.get("subtype"))
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        assert isinstance(usage, dict)
        failed = bool(record.get("is_error")) or subtype != "success"
        return (
            AgentEventV2(
                lifecycle="failed" if failed else "completed",
                session_id=session,
                result_subtype=subtype,
                num_turns=_count(record.get("num_turns")),
                input_tokens=_count(usage.get("input_tokens")),
                output_tokens=_count(usage.get("output_tokens")),
            ),
        )
    return warn("unknown_event")


def summarize_claude_stream(stdout: str, *, max_bytes: int = 10 * 1024 * 1024) -> ClaudeRunSummary:
    data = stdout.encode("utf-8", "replace")
    if len(data) > max_bytes:
        raise ClaudeStreamTooLargeError(f"claude stream exceeds {max_bytes} bytes")
    session: str | None = None
    subtype: str | None = None
    num_turns = tool_calls = tool_errors = input_tokens = output_tokens = warnings = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        for item in parse_claude_stream_line(line):
            if isinstance(item, ClaudeStreamWarning):
                warnings += 1
                continue
            session = item.session_id or session
            if item.lifecycle == "tool_call":
                tool_calls += 1
            elif item.lifecycle == "tool_result" and item.is_error:
                tool_errors += 1
            elif item.lifecycle in ("completed", "failed"):
                subtype = item.result_subtype
                num_turns = item.num_turns or 0
                input_tokens = item.input_tokens or 0
                output_tokens = item.output_tokens or 0
    return ClaudeRunSummary(
        session_id=session,
        result_subtype=subtype,
        num_turns=num_turns,
        tool_call_count=tool_calls,
        tool_error_count=tool_errors,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        warning_count=warnings,
        journal_sha256=hashlib.sha256(data).hexdigest(),
    )
