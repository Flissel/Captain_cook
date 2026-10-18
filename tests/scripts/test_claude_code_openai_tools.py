from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tools_mod = _load("claude_code_openai_tools", "claude_code_openai_tools.py")
shim_mod = _load("claude_code_openai_shim_for_tools_test", "claude_code_openai_shim.py")

WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "file-write",
        "description": "Write a note",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "meta": {"type": "object"},
                "items": {"type": "array"},
            },
            "required": ["path", "content"],
        },
    },
}
LIST_FLAT = {"name": "file-list", "description": "List", "parameters": {"type": "object", "properties": {"dir": {"type": "string"}}}}
NOARG_TOOL = {"type": "function", "function": {"name": "ping"}}


def _tools():
    return tools_mod.normalize_tools([WRITE_TOOL, LIST_FLAT, NOARG_TOOL])


def _block(payload) -> str:
    return "<tool_calls>\n" + json.dumps(payload) + "\n</tool_calls>"


def test_normalize_openai_and_flat_tools():
    out = tools_mod.normalize_tools([WRITE_TOOL, LIST_FLAT, "junk", {"type": "function"}])
    assert [t["name"] for t in out] == ["file-write", "file-list"]
    assert out[0]["description"] == "Write a note"
    assert out[1]["parameters"]["properties"]["dir"] == {"type": "string"}
    assert tools_mod.normalize_tools(None) == []


def test_tool_without_parameters():
    tools = tools_mod.normalize_tools([NOARG_TOOL])
    assert tools == [{"name": "ping", "description": "", "parameters": {"type": "object", "properties": {}}}]
    parsed = tools_mod.parse_reply(_block([{"name": "ping", "arguments": {}}]), tools)
    assert parsed.errors == []
    assert parsed.calls[0]["name"] == "ping"
    assert parsed.calls[0]["arguments"] == {}


def test_tool_choice_modes():
    f = tools_mod.tool_choice_mode
    assert f(None) == ("auto", None)
    assert f("auto") == ("auto", None)
    assert f("none") == ("none", None)
    assert f("required") == ("required", None)
    assert f({"type": "function", "function": {"name": "x"}}) == ("required", "x")


def test_prompt_section_lists_tools_and_contract():
    tools = _tools()
    section = tools_mod.tools_prompt_section(tools, "auto", None)
    for tool in tools:
        assert tool["name"] in section
        assert json.dumps(tool["parameters"]) in section
    assert "<tool_calls>" in section and "</tool_calls>" in section
    assert tools_mod.tools_prompt_section(tools, "none", None) == ""
    assert tools_mod.tools_prompt_section([], "auto", None) == ""
    forced = tools_mod.tools_prompt_section(tools, "required", "file-list")
    assert "file-list" in forced and forced != section


def test_correction_prompt_names_errors_and_contract():
    text = tools_mod.correction_prompt(["Unbekanntes Werkzeug: foo"])
    assert "Unbekanntes Werkzeug: foo" in text
    assert "<tool_calls>" in text


def test_parse_valid_single_and_multiple_calls():
    tools = _tools()
    single = tools_mod.parse_reply(_block([{"name": "file-list", "arguments": {"dir": "a"}}]), tools)
    assert single.had_block and single.errors == [] and len(single.calls) == 1
    assert single.calls[0]["id"].startswith("call_")
    multi = tools_mod.parse_reply(
        _block([{"name": "file-list", "arguments": {}}, {"name": "ping", "arguments": {}}]), tools
    )
    assert len(multi.calls) == 2
    assert multi.calls[0]["id"] != multi.calls[1]["id"]
    assert multi.calls[0]["id"].startswith("call_") and multi.calls[1]["id"].startswith("call_")


def test_parse_plain_text_no_block():
    parsed = tools_mod.parse_reply("Hallo Welt", _tools())
    assert parsed.had_block is False
    assert parsed.calls == [] and parsed.errors == []
    assert parsed.text == "Hallo Welt"


def test_parse_unknown_tool_is_error():
    parsed = tools_mod.parse_reply(_block([{"name": "rm-rf", "arguments": {}}]), _tools())
    assert parsed.had_block and parsed.calls == []
    assert any("rm-rf" in e for e in parsed.errors)


def test_parse_missing_required_and_wrong_type_are_errors():
    tools = _tools()
    missing = tools_mod.parse_reply(_block([{"name": "file-write", "arguments": {"path": "a"}}]), tools)
    assert missing.calls == [] and any("content" in e for e in missing.errors)
    wrong = tools_mod.parse_reply(
        _block([{"name": "file-write", "arguments": {"path": "a", "content": 5}}]), tools
    )
    assert wrong.calls == [] and any("content" in e for e in wrong.errors)
    boolean_as_int = tools_mod.parse_reply(
        _block([{"name": "file-write", "arguments": {"path": "a", "content": "b", "count": True}}]), tools
    )
    assert boolean_as_int.calls == [] and boolean_as_int.errors
    ok = tools_mod.parse_reply(
        _block(
            [
                {
                    "name": "file-write",
                    "arguments": {
                        "path": "a", "content": "b", "count": 1, "ratio": 1.5,
                        "flag": False, "meta": {}, "items": [],
                    },
                }
            ]
        ),
        tools,
    )
    assert ok.errors == [] and len(ok.calls) == 1


def test_parse_invalid_json_is_error():
    parsed = tools_mod.parse_reply("<tool_calls>\n[{\"name\": \"ping\", \n</tool_calls>", _tools())
    assert parsed.had_block and parsed.calls == [] and parsed.errors
    not_array = tools_mod.parse_reply('<tool_calls>{"name": "ping", "arguments": {}}</tool_calls>', _tools())
    assert not_array.calls == [] and not_array.errors
    bad_args = tools_mod.parse_reply(_block([{"name": "ping", "arguments": "x"}]), _tools())
    assert bad_args.calls == [] and bad_args.errors


def test_partial_set_is_never_returned():
    parsed = tools_mod.parse_reply(
        _block([{"name": "ping", "arguments": {}}, {"name": "nope", "arguments": {}}]), _tools()
    )
    assert parsed.calls == [] and parsed.errors


def test_two_blocks_invalid():
    one = _block([{"name": "ping", "arguments": {}}])
    parsed = tools_mod.parse_reply(one + "\n" + one, _tools())
    assert parsed.had_block and parsed.calls == [] and parsed.errors


def test_unclosed_block_is_error():
    parsed = tools_mod.parse_reply('<tool_calls>[{"name": "ping", "arguments": {}}]', _tools())
    assert parsed.had_block and parsed.calls == [] and parsed.errors


def test_block_in_code_fence():
    tools = _tools()
    fenced = "```json\n" + _block([{"name": "ping", "arguments": {}}]) + "\n```"
    parsed = tools_mod.parse_reply(fenced, tools)
    assert parsed.errors == [] and len(parsed.calls) == 1
    assert "```" not in parsed.text and parsed.text == ""
    inner = "<tool_calls>\n```json\n" + json.dumps([{"name": "ping", "arguments": {}}]) + "\n```\n</tool_calls>"
    parsed_inner = tools_mod.parse_reply(inner, tools)
    assert parsed_inner.errors == [] and len(parsed_inner.calls) == 1


def test_text_around_block_is_kept_separately():
    reply = "Ich schaue nach.\n" + _block([{"name": "ping", "arguments": {}}]) + "\nGleich."
    parsed = tools_mod.parse_reply(reply, _tools())
    assert len(parsed.calls) == 1
    assert "Ich schaue nach." in parsed.text and "Gleich." in parsed.text
    assert "<tool_calls>" not in parsed.text


def test_text_outside_block_kept_on_error():
    reply = "Vorab.\n" + _block([{"name": "nope", "arguments": {}}])
    parsed = tools_mod.parse_reply(reply, _tools())
    assert parsed.calls == [] and parsed.errors and parsed.text == "Vorab."


def test_arguments_roundtrip_exact():
    args = {"path": 'knowledge/Notes/Ä "x".md', "content": "Zeile1\n[[Wikilink]]"}
    reply = _block([{"name": "file-write", "arguments": args}])
    parsed = tools_mod.parse_reply(reply, _tools())
    assert parsed.errors == []
    assert parsed.calls[0]["arguments"] == args
    completion = tools_mod.completion_with_tool_calls(
        parsed.calls, "claude-code", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    )
    wire = completion["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(wire) == args


def test_render_tool_call_and_result():
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "liste"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "file-list", "arguments": '{"dir": "x"}'}},
                {"id": "call_b", "type": "function", "function": {"name": "ping", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "a.md\nb.md"},
        {"role": "tool", "tool_call_id": "call_b", "content": "pong"},
    ]
    system, transcript = tools_mod.render_messages_with_tools(messages)
    assert system == "SYS"
    lines = transcript.split("\n\n")
    assert lines[0] == "User: liste"
    assert lines[1] == 'Assistant (Werkzeugaufruf): file-list({"dir": "x"}) [call_a]'
    assert lines[2] == "Assistant (Werkzeugaufruf): ping({}) [call_b]"
    assert lines[3] == "Werkzeugergebnis [call_a, file-list]: a.md\nb.md"
    assert lines[4] == "Werkzeugergebnis [call_b, ping]: pong"


def test_render_assistant_text_with_tool_calls_keeps_text():
    messages = [
        {
            "role": "assistant",
            "content": "Moment",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "ping", "arguments": {"a": 1}}}],
        }
    ]
    _, transcript = tools_mod.render_messages_with_tools(messages)
    assert transcript == 'Assistant: Moment\n\nAssistant (Werkzeugaufruf): ping({"a": 1}) [c1]'


def test_render_result_truncated():
    big = "x" * 20001
    _, transcript = tools_mod.render_messages_with_tools(
        [{"role": "tool", "tool_call_id": "c", "content": big}]
    )
    assert transcript.endswith("x" * 20000 + " (gekuerzt, 20001 Zeichen)")
    exact = "y" * 20000
    _, transcript2 = tools_mod.render_messages_with_tools(
        [{"role": "tool", "tool_call_id": "c", "content": exact}]
    )
    assert "gekuerzt" not in transcript2


def test_tool_result_without_call():
    _, transcript = tools_mod.render_messages_with_tools(
        [{"role": "user", "content": "hi"}, {"role": "tool", "tool_call_id": "zzz", "content": "ok"}]
    )
    assert transcript.endswith("Werkzeugergebnis [zzz, unbekannt]: ok")


def test_render_without_tool_messages_equals_shim_render_messages():
    messages = [
        {"role": "system", "content": "S1"},
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": "antwort"},
        {"role": "developer", "content": "dev"},
        {"role": "system", "content": "S2"},
        {"role": "user", "content": "frage"},
    ]
    assert tools_mod.render_messages_with_tools(messages) == shim_mod.render_messages(messages)
    assert tools_mod.render_messages_with_tools([]) == shim_mod.render_messages([])


def test_completion_with_tool_calls_shape():
    calls = [{"id": "call_1", "name": "ping", "arguments": {"a": "ä"}}]
    usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    completion = tools_mod.completion_with_tool_calls(calls, "claude-code", usage)
    assert completion["object"] == "chat.completion"
    assert completion["model"] == "claude-code"
    assert completion["id"].startswith("chatcmpl-")
    assert isinstance(completion["created"], int)
    assert completion["usage"] == usage
    choice = completion["choices"][0]
    assert choice["index"] == 0 and choice["finish_reason"] == "tool_calls"
    message = choice["message"]
    assert message["role"] == "assistant" and message["content"] is None
    tc = message["tool_calls"][0]
    assert tc["id"] == "call_1" and tc["type"] == "function"
    assert tc["function"]["name"] == "ping"
    assert isinstance(tc["function"]["arguments"], str)
    assert json.loads(tc["function"]["arguments"]) == {"a": "ä"}


def test_stream_chunks_shape():
    calls = [
        {"id": "call_1", "name": "ping", "arguments": {}},
        {"id": "call_2", "name": "file-list", "arguments": {"dir": "x"}},
    ]
    completion = tools_mod.completion_with_tool_calls(
        calls, "m", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    chunks = tools_mod.stream_chunks_for_tool_calls(completion)
    assert len(chunks) == 2
    first, last = chunks
    for chunk in chunks:
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["id"] == completion["id"]
        assert chunk["created"] == completion["created"]
        assert chunk["model"] == "m"
    delta = first["choices"][0]["delta"]
    assert delta["role"] == "assistant"
    assert first["choices"][0]["finish_reason"] is None
    assert [t["index"] for t in delta["tool_calls"]] == [0, 1]
    assert delta["tool_calls"][1]["id"] == "call_2" and delta["tool_calls"][1]["type"] == "function"
    assert delta["tool_calls"][1]["function"]["name"] == "file-list"
    assert json.loads(delta["tool_calls"][1]["function"]["arguments"]) == {"dir": "x"}
    assert last["choices"][0]["delta"] == {}
    assert last["choices"][0]["finish_reason"] == "tool_calls"


def _clean(parsed):
    assert "<tool_calls>" not in parsed.text and "</tool_calls>" not in parsed.text
    assert "ping" not in parsed.text


def test_error_paths_do_not_leak_payload_or_tags():
    ping = _block([{"name": "ping", "arguments": {}}])
    unclosed = tools_mod.parse_reply('<tool_calls>[{"name":"ping","arguments":{}}]', _tools())
    assert unclosed.errors and unclosed.calls == []
    _clean(unclosed)
    two = tools_mod.parse_reply(ping + "\n" + ping, _tools())
    assert two.errors
    _clean(two)
    mixed = tools_mod.parse_reply("Vorab.\n" + ping + "\n<tool_calls>[{\"name\":\"ping\"", _tools())
    assert mixed.errors and mixed.text == "Vorab."
    _clean(mixed)


def test_prose_mentioning_tag_never_reaches_text():
    tools = _tools()
    expected = {
        "Ich nutze das Tag <tool_calls> nur bei Bedarf.": "Ich nutze das Tag",
        "Der Block endet mit </tool_calls> und beginnt mit <tool_calls> mitten im Satz.": "Der Block endet mit",
        'Siehe <tool_calls>[{"name":"ping","arguments":{}}]</tool_calls> im Satz.': "Siehe",
    }
    for reply, text in expected.items():
        parsed = tools_mod.parse_reply(reply, tools)
        assert parsed.had_block is True and parsed.calls == [] and parsed.errors
        assert parsed.text == text
    plain = tools_mod.parse_reply("Nur Prosa ohne Tag.", tools)
    assert plain.had_block is False and plain.errors == [] and plain.text == "Nur Prosa ohne Tag."


def test_fenced_example_inside_prose_never_reaches_text():
    reply = "So sieht ein Aufruf aus:\n```\n" + _block([{"name": "ping", "arguments": {}}]) + "\n```\nFertig."
    parsed = tools_mod.parse_reply(reply, _tools())
    assert parsed.had_block is True and parsed.calls == [] and parsed.errors
    assert parsed.text == "So sieht ein Aufruf aus:"


def test_unbalanced_opening_fence_is_wrapper():
    tools = _tools()
    closed_block = '```json\n<tool_calls>[{"name":"ping","arguments":{}}]</tool_calls>'
    ok = tools_mod.parse_reply(closed_block, tools)
    assert ok.errors == [] and len(ok.calls) == 1 and ok.text == ""
    truncated = tools_mod.parse_reply('```json\n<tool_calls>[{"name":"ping"', tools)
    assert truncated.had_block and truncated.calls == [] and truncated.errors
    assert truncated.text == ""
    _clean(truncated)


def test_midline_mention_before_later_error_block():
    reply = 'Siehe <tool_calls>[{"name":"ping","arguments":{}}]</tool_calls> im Satz.\n<tool_calls>[bad'
    parsed = tools_mod.parse_reply(reply, _tools())
    assert parsed.had_block and parsed.calls == [] and parsed.errors
    _clean(parsed)


def test_prompt_forbids_tag_in_prose_and_history_copying():
    section = tools_mod.tools_prompt_section(_tools(), "auto", None)
    assert "Schreibe das Tag <tool_calls> niemals in Erklaerungen oder Beispielen." in section
    assert "Kopiere nie die Verlaufszeilen" in section


def test_nullable_type_accepts_none():
    tools = tools_mod.normalize_tools(
        [{"name": "n", "parameters": {"type": "object", "properties": {"v": {"type": ["string", "null"]}}}}]
    )
    ok = tools_mod.parse_reply(_block([{"name": "n", "arguments": {"v": None}}]), tools)
    assert ok.errors == [] and len(ok.calls) == 1
    bad = tools_mod.parse_reply(_block([{"name": "n", "arguments": {"v": 3}}]), tools)
    assert bad.errors and bad.calls == []


def test_missing_arguments_key_is_empty_dict():
    ok = tools_mod.parse_reply(_block([{"name": "ping"}]), _tools())
    assert ok.errors == [] and ok.calls[0]["arguments"] == {}
    missing = tools_mod.parse_reply(_block([{"name": "file-write"}]), _tools())
    assert missing.calls == [] and any("path" in e for e in missing.errors)


def test_tool_choice_type_none():
    assert tools_mod.tool_choice_mode({"type": "none"}) == ("none", None)


def test_required_correction_prompt_names_tool():
    generic = tools_mod.required_correction_prompt(None)
    named = tools_mod.required_correction_prompt("file-list")
    assert generic.startswith("Ein Werkzeugaufruf ist hier Pflicht: antworte ausschliesslich mit dem <tool_calls>-Block")
    assert named.startswith('Ein Werkzeugaufruf an "file-list" ist hier Pflicht')
