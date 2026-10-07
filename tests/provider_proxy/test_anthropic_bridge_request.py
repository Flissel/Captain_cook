import json

import pytest

from agenten.provider_proxy.anthropic_bridge import (
    BridgeError,
    anthropic_request_to_chat,
    short_tool_name,
)

MODELS = frozenset({"nvidia/super", "nvidia/nano"})
LONG = "mcp__captain_artifacts__write_artifact_with_a_really_long_descriptive_name_v2"


def _request(**overrides):
    request = {
        "model": "nvidia/super",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "You are Claude Code."}, {"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": "Create hello.py"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "x"},
                {"type": "text", "text": "Writing it."},
                {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "hello.py", "content": "print('hi')"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "File written"}]},
                {"type": "text", "text": "Now run it."},
            ]},
        ],
        "tools": [{"name": "Write", "description": "Write a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}],
        "tool_choice": {"type": "auto"},
        "stream": True,
        "temperature": 0.2,
        "stop_sequences": ["</done>"],
        "metadata": {"user_id": "u"},
        "thinking": {"type": "enabled", "budget_tokens": 1000},
        "output_config": {"effort": "high"},
    }
    request.update(overrides)
    return request


def test_full_request_translation():
    out = anthropic_request_to_chat(_request(), allowed_models=MODELS)
    p = out.payload
    assert p["model"] == "nvidia/super"
    assert p["max_tokens"] == 1024
    assert p["temperature"] == 0.2
    assert p["stop"] == ["</done>"]
    assert p["stream"] is True
    assert p["stream_options"] == {"include_usage": True}
    assert p["tool_choice"] == "auto"
    for dropped in ("metadata", "thinking", "output_config", "system", "stop_sequences"):
        assert dropped not in p
    messages = p["messages"]
    assert messages[0] == {"role": "system", "content": "You are Claude Code.\n\nBe brief."}
    assert messages[1] == {"role": "user", "content": "Create hello.py"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "Writing it."
    call = messages[2]["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "Write"
    assert json.loads(call["function"]["arguments"]) == {"file_path": "hello.py", "content": "print('hi')"}
    assert messages[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "File written"}
    assert messages[4] == {"role": "user", "content": "Now run it."}
    assert p["tools"][0] == {"type": "function", "function": {"name": "Write", "description": "Write a file",
                             "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}}}


def test_unknown_model_is_rejected():
    with pytest.raises(BridgeError, match="allowlisted"):
        anthropic_request_to_chat(_request(model="claude-opus-4"), allowed_models=MODELS)


def test_images_are_rejected():
    req = _request(messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}])
    with pytest.raises(BridgeError, match="image"):
        anthropic_request_to_chat(req, allowed_models=MODELS)


def test_long_tool_names_are_shortened_and_mapped_back():
    req = _request(
        tools=[{"name": LONG, "description": "", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "go"}],
        tool_choice={"type": "tool", "name": LONG},
    )
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    short = out.payload["tools"][0]["function"]["name"]
    assert len(short) <= 64
    assert short == short_tool_name(LONG)
    assert out.tool_names[short] == LONG
    assert out.payload["tool_choice"] == {"type": "function", "function": {"name": short}}


@pytest.mark.parametrize("choice,expected", [({"type": "any"}, "required"), ({"type": "none"}, "none")])
def test_tool_choice_mapping(choice, expected):
    out = anthropic_request_to_chat(_request(tool_choice=choice), allowed_models=MODELS)
    assert out.payload["tool_choice"] == expected


def test_error_tool_result_is_prefixed():
    req = _request(messages=[
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    assert out.payload["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "Error: boom"}


@pytest.mark.parametrize("bad", [{"max_tokens": 0}, {"max_tokens": True}, {"messages": []}])
def test_invalid_shapes_are_bridge_errors(bad):
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(**bad), allowed_models=MODELS)


def test_non_stream_request_has_no_stream_options():
    out = anthropic_request_to_chat(_request(stream=False), allowed_models=MODELS)
    assert "stream" not in out.payload and "stream_options" not in out.payload


# Fix round 1: Malformed input validation
def test_stop_sequences_must_be_list_of_non_empty_strings():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences="abc"), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=[1, 2]), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=[""]), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=5), allowed_models=MODELS)


def test_thinking_only_assistant_message_is_skipped():
    req = _request(messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "pondering"}]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    # Should skip the thinking-only assistant message (system message is still there)
    msgs = out.payload["messages"]
    # First is system, second is user, no assistant
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert len(msgs) == 2


def test_empty_list_assistant_message_is_skipped():
    req = _request(messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": []},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    # Should skip the empty assistant message (system message is still there)
    msgs = out.payload["messages"]
    # First is system, second is user, no assistant
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "hello"}
    assert len(msgs) == 2


def test_tool_calls_without_text_content_none_requires_result():
    # Assistant with tool_call but no result is invalid (pending tools at request end)
    with pytest.raises(BridgeError, match="pending|unanswered"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {"x": "y"}},
            ]},
        ]), allowed_models=MODELS)


def test_tool_use_input_must_be_dict_or_none():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": "string"}]},
        ]), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": [1, 2]}]},
        ]), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": 42}]},
        ]), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": True}]},
        ]), allowed_models=MODELS)


def test_empty_user_content_list_is_error():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": []},
        ]), allowed_models=MODELS)


def test_tool_result_without_preceding_tool_calls_is_error():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "result"},
            ]},
        ]), allowed_models=MODELS)


def test_tool_result_id_must_match_preceding_tool_call():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "result"},
            ]},
        ]), allowed_models=MODELS)


def test_duplicate_tool_result_for_same_id_is_error():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "result1"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "result2"},
            ]},
        ]), allowed_models=MODELS)


def test_multiple_tool_results_for_multiple_tool_calls_in_one_turn_is_valid():
    req = _request(messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "written"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "ran"},
        ]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    msgs = out.payload["messages"]
    # Should have: [assistant with t1 & t2], [tool t1], [tool t2]
    assert len([m for m in msgs if m["role"] == "tool"]) == 2


def test_tool_choice_without_tools_is_error_unless_auto_or_none():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(tools=None, tool_choice={"type": "tool", "name": "Write"}), allowed_models=MODELS)
    # auto and none should drop tool_choice
    out = anthropic_request_to_chat(_request(tools=None, tool_choice={"type": "auto"}), allowed_models=MODELS)
    assert "tool_choice" not in out.payload
    out = anthropic_request_to_chat(_request(tools=None, tool_choice={"type": "none"}), allowed_models=MODELS)
    assert "tool_choice" not in out.payload


def test_temperature_must_be_number_not_bool():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(temperature=True), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(temperature=False), allowed_models=MODELS)


def test_top_p_must_be_number_not_bool():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(top_p=True), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(top_p=False), allowed_models=MODELS)


def test_stream_must_be_bool_if_present():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stream="yes"), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stream=1), allowed_models=MODELS)


# Fix round 2: Strict tool_call pairing and collision detection
def test_chat_name_collision_is_error():
    # Two tools: one long name and one whose literal name equals short_tool_name(long_name)
    long_name = "mcp__captain_artifacts__write_artifact_with_a_really_long_descriptive_name_v2"
    short = short_tool_name(long_name)
    # Create a collision: second tool name IS the short form of the first
    with pytest.raises(BridgeError, match="collision"):
        anthropic_request_to_chat(_request(
            tools=[
                {"name": long_name, "description": "", "input_schema": {"type": "object"}},
                {"name": short, "description": "", "input_schema": {"type": "object"}},
            ],
            messages=[{"role": "user", "content": "go"}],
        ), allowed_models=MODELS)


def test_invalid_tool_choice_type_is_error():
    with pytest.raises(BridgeError, match="unsupported"):
        anthropic_request_to_chat(_request(
            tool_choice={"type": "bogus"},
        ), allowed_models=MODELS)


def test_temperature_string_is_error():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(temperature="hot"), allowed_models=MODELS)


def test_top_p_string_is_error():
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(top_p="x"), allowed_models=MODELS)


def test_stop_sequences_falsy_values_rejected():
    # Empty string in stop_sequences should raise
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=[""]), allowed_models=MODELS)
    # False in stop_sequences should raise
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=[False]), allowed_models=MODELS)
    # 0 in stop_sequences should raise
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=[0]), allowed_models=MODELS)
    # Falsy non-list values should raise
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences={}), allowed_models=MODELS)
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(stop_sequences=""), allowed_models=MODELS)


def test_strict_tool_pairing_multiple_assistants_with_different_tools():
    # Two assistant turns with tool_uses: second assistant message arrives while first tools pending
    with pytest.raises(BridgeError, match="pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
            ]},
        ]), allowed_models=MODELS)


def test_strict_tool_pairing_partial_answers():
    # Two tool_uses expected, only one answered
    with pytest.raises(BridgeError, match="pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
            ]},
        ]), allowed_models=MODELS)


def test_strict_tool_pairing_user_text_only_while_pending():
    # User message with text only while tools are pending
    with pytest.raises(BridgeError, match="pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
            ]},
        ]), allowed_models=MODELS)


def test_strict_tool_pairing_request_ends_with_pending_tools():
    # Request ends with an assistant message that has tool_uses
    with pytest.raises(BridgeError, match="unanswered|pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            ]},
        ]), allowed_models=MODELS)


def test_strict_tool_pairing_skipped_thinking_only_leaves_pending():
    # Thinking-only message is skipped (emits nothing), pending still valid
    req = _request(messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"},
        ]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    # Should succeed: thinking-only is skipped, t1 is still pending and answered
    msgs = out.payload["messages"]
    # Verify structure
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "t1" for m in msgs)


def test_strict_tool_pairing_realistic_multi_turn():
    # Realistic Claude Code conversation:
    # User asks → Assistant text + 2 tool_uses → User 2 results + text → Assistant text + 1 tool_use → User 1 result
    req = _request(messages=[
        {"role": "user", "content": "Please do two things"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll do them."},
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "x.py"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "written"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "files"},
            {"type": "text", "text": "Great! Now run one more."},
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Running it."},
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "python x.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "result"},
        ]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    msgs = out.payload["messages"]
    # Verify exact order (system + user + assistant + tool + tool + user + assistant + tool + user)
    non_sys = msgs[1:]  # Skip system
    assert non_sys[0]["role"] == "user"
    assert non_sys[0]["content"] == "Please do two things"
    assert non_sys[1]["role"] == "assistant"
    assert non_sys[1]["content"] == "I'll do them."
    assert len(non_sys[1]["tool_calls"]) == 2
    assert non_sys[2]["role"] == "tool"
    assert non_sys[2]["tool_call_id"] == "t1"
    assert non_sys[3]["role"] == "tool"
    assert non_sys[3]["tool_call_id"] == "t2"
    assert non_sys[4]["role"] == "user"
    assert non_sys[4]["content"] == "Great! Now run one more."
    assert non_sys[5]["role"] == "assistant"
    assert non_sys[5]["content"] == "Running it."
    assert len(non_sys[5]["tool_calls"]) == 1
    assert non_sys[5]["tool_calls"][0]["id"] == "t3"
    assert non_sys[6]["role"] == "tool"
    assert non_sys[6]["tool_call_id"] == "t3"


# Fix round 3: String-content user messages and duplicate tool_use ids
def test_string_content_user_message_while_pending():
    # User message with string content (not list) while tools pending → BridgeError
    with pytest.raises(BridgeError, match="pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            ]},
            {"role": "user", "content": "hi"},  # Plain string, not list with tool_result
        ]), allowed_models=MODELS)


def test_string_content_user_probe():
    # Exact probe from brief: user "go" → assistant tool_use t1 → user string "hi" → user tool_result t1
    with pytest.raises(BridgeError, match="pending"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            ]},
            {"role": "user", "content": "hi"},  # String content while t1 pending
        ]), allowed_models=MODELS)


def test_duplicate_tool_use_ids_in_assistant_message():
    # R18: Duplicate tool_use ids within one assistant message → BridgeError
    with pytest.raises(BridgeError, match="duplicate|already"):
        anthropic_request_to_chat(_request(messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},  # Same id
            ]},
        ]), allowed_models=MODELS)


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "Write", "input_schema": "not-an-object"},
        {"name": "Write", "input_schema": ["type", "object"]},
        {"name": "Write", "input_schema": None},
        {"name": "Write", "description": 42, "input_schema": {"type": "object"}},
        {"name": "Write", "description": None, "input_schema": {"type": "object"}},
        {"name": "Write", "description": ["x"], "input_schema": {"type": "object"}},
    ],
)
def test_tool_schema_and_description_types_are_enforced(tool):
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(tools=[tool]), allowed_models=MODELS)


def test_tool_without_description_gets_an_empty_one():
    out = anthropic_request_to_chat(
        _request(tools=[{"name": "Write", "input_schema": {"type": "object"}}]), allowed_models=MODELS
    )
    assert out.payload["tools"][0]["function"]["description"] == ""
    assert out.payload["tools"][0]["function"]["parameters"] == {"type": "object"}
