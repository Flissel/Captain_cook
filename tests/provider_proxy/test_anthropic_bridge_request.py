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
