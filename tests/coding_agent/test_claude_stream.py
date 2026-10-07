import json

import pytest

from agenten.coding_agent.claude_stream import (
    AgentEventV2,
    ClaudeStreamTooLargeError,
    ClaudeStreamWarning,
    parse_claude_stream_line,
    summarize_claude_stream,
)

INIT = {"type": "system", "subtype": "init", "session_id": "s-1", "model": "nvidia/x", "tools": ["Bash"]}
ASSIST_TOOL = {
    "type": "assistant",
    "session_id": "s-1",
    "message": {"content": [
        {"type": "text", "text": "I will write the file"},
        {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "a.py", "content": "SECRET-CONTENT"}},
    ]},
}
TOOL_RESULT = {
    "type": "user",
    "session_id": "s-1",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": True}]},
}
ASSIST_TEXT = {"type": "assistant", "session_id": "s-1", "message": {"content": [{"type": "text", "text": "done"}]}}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "session_id": "s-1",
    "total_cost_usd": 9.99, "usage": {"input_tokens": 120, "output_tokens": 45}, "result": "done",
}


def _lines(*records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def test_init_becomes_started():
    (event,) = parse_claude_stream_line(json.dumps(INIT))
    assert event == AgentEventV2(lifecycle="started", session_id="s-1")


def test_assistant_tool_use_becomes_tool_call_without_content():
    (event,) = parse_claude_stream_line(json.dumps(ASSIST_TOOL))
    assert event.lifecycle == "tool_call"
    assert event.item_id == "toolu_1"
    assert event.item_type == "Write"
    assert "SECRET-CONTENT" not in event.model_dump_json()


def test_assistant_text_only_becomes_turn_completed():
    (event,) = parse_claude_stream_line(json.dumps(ASSIST_TEXT))
    assert event.lifecycle == "turn_completed"


def test_tool_result_carries_error_flag():
    (event,) = parse_claude_stream_line(json.dumps(TOOL_RESULT))
    assert event.lifecycle == "tool_result"
    assert event.is_error is True


def test_result_success_and_error_subtypes():
    (ok,) = parse_claude_stream_line(json.dumps(RESULT))
    assert ok.lifecycle == "completed"
    assert (ok.num_turns, ok.input_tokens, ok.output_tokens) == (3, 120, 45)
    failed = dict(RESULT, subtype="error_max_turns", is_error=True)
    (bad,) = parse_claude_stream_line(json.dumps(failed))
    assert bad.lifecycle == "failed"
    assert bad.result_subtype == "error_max_turns"


@pytest.mark.parametrize(
    "line,kind",
    [("not json", "malformed_json"), ("[1,2]", "invalid_event"), (json.dumps({"type": "mystery"}), "unknown_event")],
)
def test_bad_lines_become_warnings(line, kind):
    (warning,) = parse_claude_stream_line(line)
    assert isinstance(warning, ClaudeStreamWarning)
    assert warning.warning_type == kind
    assert len(warning.line_sha256) == 64


def test_summary_counts_and_hashes():
    stdout = _lines(INIT, ASSIST_TOOL, TOOL_RESULT, ASSIST_TEXT, RESULT) + "garbage\n"
    summary = summarize_claude_stream(stdout)
    assert summary.session_id == "s-1"
    assert summary.result_subtype == "success"
    assert summary.num_turns == 3
    assert summary.tool_call_count == 1
    assert summary.tool_error_count == 1
    assert (summary.input_tokens, summary.output_tokens) == (120, 45)
    assert summary.warning_count == 1
    assert len(summary.journal_sha256) == 64


def test_summary_without_result_has_no_subtype():
    summary = summarize_claude_stream(_lines(INIT, ASSIST_TEXT))
    assert summary.session_id == "s-1"
    assert summary.result_subtype is None


def test_empty_stream_has_no_session():
    assert summarize_claude_stream("").session_id is None


def test_oversized_stream_is_rejected():
    with pytest.raises(ClaudeStreamTooLargeError):
        summarize_claude_stream("x" * 101, max_bytes=100)
