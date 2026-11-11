"""OpenAI tool calling through the Claude Code subscription shim.

The CLI is replaced by a fake ``subprocess.run`` that records argv and
returns the JSON the real CLI prints; the live round trip is in
``tests/live/test_live_claude_code_shim_tools.py``.
"""

from __future__ import annotations

import json
import subprocess
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude_code_openai_shim.py"

ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}
LOOKUP_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look up a word.",
        "parameters": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
    },
}


def load_shim() -> ModuleType:
    spec = spec_from_file_location("claude_code_openai_shim", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCli:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.argv: list[str] = []
        self.stdin = ""
        self.system_prompt = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argv = list(argv)
        self.stdin = kwargs["input"]
        if "--system-prompt-file" in argv:
            path = argv[argv.index("--system-prompt-file") + 1]
            self.system_prompt = Path(path).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps(self.payload), "")

    def flag(self, name: str) -> str:
        return self.argv[self.argv.index(name) + 1]


def structured(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(value),
        "structured_output": value,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.fixture()
def shim(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module = load_shim()
    monkeypatch.setattr(module, "resolve_cli", lambda: "claude")
    monkeypatch.delenv("CAPTAIN_ARTIFACT_ROOT", raising=False)
    return module


def complete(shim: ModuleType, fake: FakeCli, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(shim.subprocess, "run", fake)
    return shim.complete_chat(body, timeout=30.0)


def test_a_tool_call_comes_back_in_openai_shape(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli(structured({"tool_calls": [{"name": "add", "arguments": {"a": 2, "b": 3}}]}))

    completion = complete(
        shim,
        fake,
        {"model": "claude-code", "messages": [{"role": "user", "content": "2+3?"}], "tools": [ADD_TOOL]},
        monkeypatch,
    )

    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    (call,) = choice["message"]["tool_calls"]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "add"
    assert json.loads(call["function"]["arguments"]) == {"a": 2, "b": 3}


def test_the_envelope_schema_pins_tool_names_and_their_parameters(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCli(structured({"content": "done"}))

    complete(
        shim,
        fake,
        {"messages": [{"role": "user", "content": "hi"}], "tools": [ADD_TOOL, LOOKUP_TOOL]},
        monkeypatch,
    )

    schema = json.loads(fake.flag("--json-schema"))
    variants = schema["properties"]["tool_calls"]["items"]["anyOf"]
    assert [v["properties"]["name"]["const"] for v in variants] == ["add", "lookup"]
    assert variants[0]["properties"]["arguments"] == ADD_TOOL["function"]["parameters"]
    assert schema["properties"]["content"] == {"type": "string"}
    assert schema["additionalProperties"] is False
    # Claude Code's own tools stay off; the caller executes the functions.
    assert fake.flag("--tools") == ""
    assert '"add"' in fake.system_prompt and "Add two integers." in fake.system_prompt


def test_a_final_answer_is_plain_content(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli(structured({"content": "The sum is 5."}))

    completion = complete(
        shim, fake, {"messages": [{"role": "user", "content": "2+3?"}], "tools": [ADD_TOOL]}, monkeypatch
    )

    choice = completion["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {"role": "assistant", "content": "The sum is 5."}


def test_required_tool_choice_drops_the_content_branch(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli(structured({"tool_calls": [{"name": "lookup", "arguments": {"word": "x"}}]}))

    complete(
        shim,
        fake,
        {
            "messages": [{"role": "user", "content": "x"}],
            "tools": [ADD_TOOL, LOOKUP_TOOL],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        },
        monkeypatch,
    )

    schema = json.loads(fake.flag("--json-schema"))
    assert "content" not in schema["properties"]
    assert schema["required"] == ["tool_calls"]
    assert schema["properties"]["tool_calls"]["minItems"] == 1
    variants = schema["properties"]["tool_calls"]["items"]["anyOf"]
    assert [v["properties"]["name"]["const"] for v in variants] == ["lookup"]


def test_tool_choice_none_keeps_the_plain_text_path(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli({"type": "result", "is_error": False, "result": "plain", "usage": {}})

    completion = complete(
        shim,
        fake,
        {"messages": [{"role": "user", "content": "x"}], "tools": [ADD_TOOL], "tool_choice": "none"},
        monkeypatch,
    )

    assert "--json-schema" not in fake.argv
    assert completion["choices"][0]["message"]["content"] == "plain"


def test_a_call_to_an_unoffered_tool_is_rejected(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli(structured({"tool_calls": [{"name": "rm_rf", "arguments": {}}]}))

    with pytest.raises(shim.ShimError, match="unoffered tool"):
        complete(shim, fake, {"messages": [{"role": "user", "content": "x"}], "tools": [ADD_TOOL]}, monkeypatch)


def test_an_empty_envelope_is_rejected(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli(structured({}))

    with pytest.raises(shim.ShimError, match="neither"):
        complete(shim, fake, {"messages": [{"role": "user", "content": "x"}], "tools": [ADD_TOOL]}, monkeypatch)


def test_missing_structured_output_is_an_error(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli({"type": "result", "is_error": False, "result": "I would call add.", "usage": {}})

    with pytest.raises(shim.ShimError, match="structured"):
        complete(shim, fake, {"messages": [{"role": "user", "content": "x"}], "tools": [ADD_TOOL]}, monkeypatch)


def test_prior_tool_calls_and_results_are_rendered_into_the_transcript(shim: ModuleType) -> None:
    _, transcript = shim.render_messages(
        [
            {"role": "user", "content": "2+3?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "5"},
        ]
    )

    assert "User: 2+3?" in transcript
    assert 'Assistant called add with {"a": 2, "b": 3} (call call_1)' in transcript
    assert "Tool result for call_1 (add): 5" in transcript
    assert "Assistant: None" not in transcript


def test_tool_calls_stream_as_one_delta(shim: ModuleType) -> None:
    completion = {
        "id": "chatcmpl-1",
        "created": 1,
        "model": "claude-code",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "add", "arguments": "{}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    chunks = shim.stream_chunks(completion)

    first, final = chunks
    delta = first["choices"][0]["delta"]
    assert delta["tool_calls"][0]["index"] == 0
    assert delta["tool_calls"][0]["function"]["name"] == "add"
    assert "content" not in delta
    assert final["choices"][0]["finish_reason"] == "tool_calls"


def test_tool_choice_naming_an_unoffered_tool_is_a_client_error(shim: ModuleType) -> None:
    with pytest.raises(shim.ShimRequestError, match="not offered"):
        shim.tool_envelope_schema([ADD_TOOL], {"type": "function", "function": {"name": "lookup"}})


# --- structured output -------------------------------------------------------

REPORT_SCHEMA = {
    "$defs": {"Finding": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"$ref": "#/$defs/Finding"}},
    },
    "required": ["summary", "findings"],
    "additionalProperties": False,
}


def test_a_json_schema_response_format_is_enforced_by_the_cli(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {"summary": "ok", "findings": [{"text": "a"}]}
    fake = FakeCli({**structured(report), "result": "```json\n" + json.dumps(report) + "\n```"})

    completion = complete(
        shim,
        fake,
        {
            "messages": [{"role": "user", "content": "audit"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "Report", "schema": REPORT_SCHEMA, "strict": True},
            },
        },
        monkeypatch,
    )

    assert json.loads(fake.flag("--json-schema")) == REPORT_SCHEMA
    assert fake.flag("--tools") == ""
    # The validated object, never the fenced prose the model printed.
    assert json.loads(completion["choices"][0]["message"]["content"]) == report


def test_a_json_schema_answer_without_structured_output_is_an_error(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCli({"type": "result", "is_error": False, "result": "{}", "usage": {}})

    with pytest.raises(shim.ShimError, match="structured"):
        complete(
            shim,
            fake,
            {
                "messages": [{"role": "user", "content": "audit"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "R", "schema": REPORT_SCHEMA}},
            },
            monkeypatch,
        )


def test_json_object_mode_strips_a_code_fence(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCli({"type": "result", "is_error": False, "result": '```json\n{"a": 1}\n```', "usage": {}})

    completion = complete(
        shim,
        fake,
        {"messages": [{"role": "user", "content": "x"}], "response_format": {"type": "json_object"}},
        monkeypatch,
    )

    assert "--json-schema" not in fake.argv
    assert json.loads(completion["choices"][0]["message"]["content"]) == {"a": 1}


def test_the_plain_text_path_offers_no_built_in_tools(shim: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the CLI's own tools are offered and only refused for lack
    of a TTY; the refusal then comes back as the answer ("I need your
    permission to read that file")."""
    fake = FakeCli({"type": "result", "is_error": False, "result": "plain", "usage": {}})

    complete(shim, fake, {"messages": [{"role": "user", "content": "x"}]}, monkeypatch)

    assert fake.flag("--tools") == ""
    assert "--allowedTools" not in fake.argv


def test_the_artifact_store_adds_only_its_mcp_tools(
    shim: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mcp_script = tmp_path / "captain_artifact_mcp.py"
    mcp_script.write_text("", encoding="utf-8")
    monkeypatch.setenv("CAPTAIN_ARTIFACT_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("CAPTAIN_ARTIFACT_MCP", str(mcp_script))
    fake = FakeCli({"type": "result", "is_error": False, "result": "plain", "usage": {}})

    complete(shim, fake, {"messages": [{"role": "user", "content": "x"}]}, monkeypatch)

    assert fake.flag("--tools") == ""
    start = fake.argv.index("--allowedTools") + 1
    assert fake.argv[start : start + 3] == [
        "mcp__captain__write_artifact",
        "mcp__captain__read_artifact",
        "mcp__captain__get_contract_schema",
    ]
    assert fake.argv.count("--tools") == 1
