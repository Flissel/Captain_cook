import json

import pytest

from agenten.provider_proxy.anthropic_bridge import (
    ChatStreamToAnthropic,
    UpstreamFormatError,
    chat_completion_to_anthropic,
    short_tool_name,
    to_sse,
)

LONG = "mcp__captain_artifacts__write_artifact_with_a_really_long_descriptive_name_v2"


def _chunk(delta=None, finish=None, usage=None):
    chunk = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        chunk["usage"] = usage
        if delta is None and finish is None:
            chunk["choices"] = []
    return chunk


def _run(chunks, tool_names=None):
    translator = ChatStreamToAnthropic(model="nvidia/super", tool_names=tool_names or {})
    events = []
    for chunk in chunks:
        events += translator.feed(chunk)
    events += translator.finish()
    return translator, events


def test_text_stream_produces_well_formed_event_sequence():
    translator, events = _run([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Hel"}),
        _chunk({"reasoning_content": "secret thoughts"}),
        _chunk({"content": "lo"}),
        _chunk(finish="stop"),
        _chunk(usage={"prompt_tokens": 11, "completion_tokens": 4}),
    ])
    assert [e["type"] for e in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[1]["content_block"] == {"type": "text", "text": ""}
    assert "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta") == "Hello"
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    assert events[-2]["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert (translator.input_tokens, translator.output_tokens) == (11, 4)
    assert "secret thoughts" not in json.dumps(events)


def test_fragmented_tool_call_becomes_tool_use_with_input_json_deltas():
    short = short_tool_name(LONG)
    _, events = _run([
        _chunk({"content": "Let me write."}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": short, "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}),
        _chunk(finish="tool_calls"),
    ], tool_names={short: LONG})
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "text"
    assert starts[1]["index"] == 1
    assert starts[1]["content_block"] == {"type": "tool_use", "id": "call_1", "name": LONG, "input": {}}
    partial = "".join(e["delta"]["partial_json"] for e in events
                      if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
    assert json.loads(partial) == {"path": "a.py"}
    stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
    assert stops == [0, 1]
    assert events[-2]["delta"]["stop_reason"] == "tool_use"


def test_two_tool_calls_get_separate_blocks():
    _, events = _run([
        _chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Read", "arguments": "{}"}}]}),
        _chunk({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "Bash", "arguments": "{}"}}]}),
        _chunk(finish="tool_calls"),
    ])
    names = [e["content_block"]["name"] for e in events if e["type"] == "content_block_start"]
    assert names == ["Read", "Bash"]


def test_length_maps_to_max_tokens():
    _, events = _run([_chunk({"content": "x"}), _chunk(finish="length")])
    assert events[-2]["delta"]["stop_reason"] == "max_tokens"


def test_feed_after_finish_is_an_upstream_error():
    translator = ChatStreamToAnthropic(model="m", tool_names={})
    translator.feed(_chunk({"content": "x"}, finish="stop"))
    translator.finish()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": "late"}))


def test_sse_framing():
    assert to_sse({"type": "message_stop"}) == b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def test_non_stream_completion_translation():
    short = short_tool_name(LONG)
    message = chat_completion_to_anthropic({
        "choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": short, "arguments": '{"x":1}'}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }, model="nvidia/super", tool_names={short: LONG})
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["model"] == "nvidia/super"
    assert message["content"] == [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "c1", "name": LONG, "input": {"x": 1}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 7, "output_tokens": 2}


def test_non_stream_invalid_tool_arguments_are_upstream_errors():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "Bash", "arguments": "{broken"}}]}, "finish_reason": "tool_calls"}]},
            model="m", tool_names={})


def _tool_chunk(index, *, id=None, name=None, arguments=None):
    call = {"index": index, "function": {}}
    if id is not None:
        call["id"] = id
    if name is not None:
        call["function"]["name"] = name
    if arguments is not None:
        call["function"]["arguments"] = arguments
    return _chunk({"tool_calls": [call]})


def _translator():
    return ChatStreamToAnthropic(model="m", tool_names={})


def test_truncated_stream_with_cut_tool_arguments_fails_at_finish():
    translator = _translator()
    translator.feed(_tool_chunk(0, id="a", name="Bash", arguments='{"command": "ls'))
    with pytest.raises(UpstreamFormatError):
        translator.finish()


def test_stream_without_finish_reason_fails_at_finish():
    translator = _translator()
    translator.feed(_chunk({"content": "partial"}))
    with pytest.raises(UpstreamFormatError):
        translator.finish()


def test_zero_chunk_stream_fails_at_finish():
    with pytest.raises(UpstreamFormatError):
        _translator().finish()


def test_stream_tool_arguments_must_be_a_json_object():
    translator = _translator()
    translator.feed(_tool_chunk(0, id="a", name="Bash", arguments="[1"))
    translator.feed(_tool_chunk(0, arguments="]"))
    translator.feed(_chunk(finish="tool_calls"))
    with pytest.raises(UpstreamFormatError):
        translator.finish()


def test_stream_tool_arguments_not_json_at_block_switch_fail():
    translator = _translator()
    translator.feed(_tool_chunk(0, id="a", name="Bash", arguments="{oops"))
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": "text after"}))


def test_stream_empty_tool_arguments_are_an_empty_object():
    _, events = _run([
        _tool_chunk(0, id="a", name="Bash", arguments=""),
        _chunk(finish="tool_calls"),
    ])
    assert events[-2]["delta"]["stop_reason"] == "tool_use"


def test_tool_call_without_index_is_an_upstream_error():
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"tool_calls": [{"id": "a", "function": {"name": "Bash", "arguments": "{}"}}]}))


def test_delta_for_an_already_closed_tool_index_is_an_upstream_error():
    translator = _translator()
    translator.feed(_tool_chunk(0, id="a", name="Read", arguments="{}"))
    translator.feed(_tool_chunk(1, id="b", name="Bash", arguments="{}"))
    with pytest.raises(UpstreamFormatError):
        translator.feed(_tool_chunk(0, arguments=" "))


def test_non_stream_tool_arguments_dict_is_an_upstream_error():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "Bash", "arguments": {"command": "ls"}}}]},
            "finish_reason": "tool_calls"}]}, model="m", tool_names={})


def test_non_stream_tool_arguments_non_object_json_is_an_upstream_error():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "Bash", "arguments": "[1]"}}]},
            "finish_reason": "tool_calls"}]}, model="m", tool_names={})


def test_non_stream_absent_tool_arguments_become_an_empty_object():
    message = chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
        {"id": "c", "function": {"name": "Bash"}}]}, "finish_reason": "tool_calls"}]},
        model="m", tool_names={})
    assert message["content"] == [{"type": "tool_use", "id": "c", "name": "Bash", "input": {}}]


def test_non_stream_empty_tool_name_is_an_upstream_error():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}, model="m", tool_names={})


def test_non_stream_choice_without_message_is_an_upstream_error():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"finish_reason": "stop"}]}, model="m", tool_names={})


def test_unknown_finish_reason_is_an_upstream_error_in_stream():
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": "x"}, finish="error"))


def test_unknown_finish_reason_is_an_upstream_error_one_shot():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "abort"}]}, model="m", tool_names={})


def test_stop_after_tool_use_block_reports_tool_use_in_stream():
    _, events = _run([_tool_chunk(0, id="a", name="Bash", arguments="{}"), _chunk(finish="stop")])
    assert events[-2]["delta"]["stop_reason"] == "tool_use"


def test_stop_after_tool_use_block_reports_tool_use_one_shot():
    message = chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
        {"id": "c", "function": {"name": "Bash", "arguments": "{}"}}]}, "finish_reason": "stop"}]},
        model="m", tool_names={})
    assert message["stop_reason"] == "tool_use"


def test_partial_usage_updates_do_not_clobber_earlier_values():
    translator, events = _run([
        _chunk({"content": "x"}, finish="stop"),
        _chunk(usage={"prompt_tokens": 10, "completion_tokens": 3}),
        _chunk(usage={"completion_tokens": 5}),
    ])
    assert (translator.input_tokens, translator.output_tokens) == (10, 5)
    assert events[-2]["usage"] == {"input_tokens": 10, "output_tokens": 5}


@pytest.mark.parametrize("bad", ["10", -1, True, 1.5])
def test_invalid_usage_value_is_an_upstream_error(bad):
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk(usage={"prompt_tokens": bad}))


def test_invalid_usage_value_is_an_upstream_error_one_shot():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
             "usage": {"completion_tokens": "3"}}, model="m", tool_names={})


def test_second_choice_index_is_an_upstream_error():
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed({"choices": [{"index": 1, "delta": {"content": "x"}, "finish_reason": None}]})


def test_non_string_delta_content_is_an_upstream_error():
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": 5}))


def test_non_string_message_content_is_an_upstream_error():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic(
            {"choices": [{"message": {"content": ["x"]}, "finish_reason": "stop"}]}, model="m", tool_names={})


def test_translator_stays_failed_after_a_feed_error():
    translator = _translator()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": 5}))
    with pytest.raises(UpstreamFormatError, match="translator failed"):
        translator.finish()
    with pytest.raises(UpstreamFormatError, match="translator failed"):
        translator.feed(_chunk({"content": "ok"}))


def test_text_tool_text_switching_has_matching_block_indices():
    _, events = _run([
        _chunk({"content": "a"}),
        _tool_chunk(0, id="t", name="Bash", arguments="{}"),
        _chunk({"content": "b"}),
        _chunk(finish="stop"),
    ])
    starts = [e["index"] for e in events if e["type"] == "content_block_start"]
    stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
    assert starts == [0, 1, 2]
    assert stops == [0, 1, 2]
    kinds = [e["content_block"]["type"] for e in events if e["type"] == "content_block_start"]
    assert kinds == ["text", "tool_use", "text"]
