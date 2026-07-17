from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agenten.execution.codex_events import (
    CodexParseWarning,
    CodexProcessEvent,
    parse_codex_jsonl,
)


@pytest.mark.parametrize(
    ("line", "lifecycle", "session_id"),
    [
        ('{"type":"thread.started","thread_id":"019f-thread-1"}', "started", "019f-thread-1"),
        ('{"type":"turn.started"}', "turn_started", None),
        ('{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":3,"output_tokens":5}}', "turn_completed", None),
        ('{"type":"error","message":"provider detail must be redacted"}', "failed", None),
    ],
)
def test_real_codex_jsonl_lifecycle_records_become_typed_session_events(
    line: str, lifecycle: str, session_id: str | None
) -> None:
    event = parse_codex_jsonl(line)

    assert isinstance(event, CodexProcessEvent)
    assert event.event_type == "codex_session"
    assert event.lifecycle == lifecycle
    assert event.session_id == session_id
    assert "provider detail must be redacted" not in event.model_dump_json()


def test_completed_item_keeps_structure_but_never_serializes_message_content() -> None:
    line = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": "untrusted-sensitive-content",
            },
        }
    )

    event = parse_codex_jsonl(line)

    assert isinstance(event, CodexProcessEvent)
    assert event.lifecycle == "item_completed"
    assert event.item_id == "item-1"
    assert event.item_type == "agent_message"
    assert "untrusted-sensitive-content" not in event.model_dump_json()


@pytest.mark.parametrize(
    ("line", "warning_type"),
    [
        ('{"type":', "malformed_json"),
        ('{"type":"future.lifecycle","secret":"untrusted-sensitive-content"}', "unknown_event"),
        ('{"type":"thread.started"}', "invalid_event"),
    ],
)
def test_malformed_unknown_and_invalid_lines_become_redacted_typed_warnings(
    line: str, warning_type: str
) -> None:
    warning = parse_codex_jsonl(line)

    assert isinstance(warning, CodexParseWarning)
    assert warning.event_type == "codex_session_warning"
    assert warning.warning_type == warning_type
    assert len(warning.line_sha256) == 64
    assert line not in warning.model_dump_json()
    assert "untrusted-sensitive-content" not in warning.model_dump_json()


def test_parser_outputs_are_immutable() -> None:
    event = parse_codex_jsonl('{"type":"turn.started"}')
    assert isinstance(event, CodexProcessEvent)

    with pytest.raises(ValidationError):
        event.lifecycle = "failed"
