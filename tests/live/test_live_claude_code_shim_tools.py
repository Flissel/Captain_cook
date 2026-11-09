"""Live: a full OpenAI tool-call round trip through the Claude Code shim.

Runs the real shim in-process on a free loopback port against the real
Claude Code CLI (the operator's subscription) and drives it with the same
AutoGen OpenAI client Captain uses. Opt in with ``-m live``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest
from autogen_core import CancellationToken
from autogen_core.models import (
    AssistantMessage,
    FunctionExecutionResult,
    FunctionExecutionResultMessage,
    UserMessage,
)
from autogen_core.tools import FunctionTool

from agenten.llm.model_client import build_model_client
from config.llm_provider import resolve_provider

pytestmark = pytest.mark.live

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude_code_openai_shim.py"


def _load_shim() -> ModuleType:
    spec = spec_from_file_location("claude_code_openai_shim_live", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def shim_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # No artifact MCP server: this test is about function calling only.
    monkeypatch.delenv("CAPTAIN_ARTIFACT_ROOT", raising=False)
    shim = _load_shim()
    shim.Handler.timeout_seconds = 240.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@pytest.mark.asyncio
async def test_claude_code_provider_calls_a_tool_and_answers_with_its_result(shim_url: str) -> None:
    profile = resolve_provider(
        {
            "CAPTAIN_LLM_PROVIDER": "claude-code",
            "CAPTAIN_LLM_BASE_URL": shim_url,
            "CAPTAIN_MODEL": "claude-code-haiku",
        }
    )
    assert profile.tool_calling is True
    client = build_model_client(profile=profile)
    tool = FunctionTool(add, description="Add two integers.")
    question = UserMessage(
        content="What is 17 + 25? Use the add tool, then answer with the number only.",
        source="user",
    )
    try:
        first = await client.create([question], tools=[tool])
        assert first.finish_reason == "function_calls"
        assert isinstance(first.content, list) and len(first.content) == 1
        call = first.content[0]
        assert call.name == "add"
        arguments = json.loads(call.arguments)
        assert arguments == {"a": 17, "b": 25}

        value = await tool.run_json(arguments, CancellationToken())
        second = await client.create(
            [
                question,
                AssistantMessage(content=[call], source="assistant"),
                FunctionExecutionResultMessage(
                    content=[
                        FunctionExecutionResult(
                            call_id=call.id,
                            content=tool.return_value_as_string(value),
                            name=call.name,
                            is_error=False,
                        )
                    ]
                ),
            ],
            tools=[tool],
        )
    finally:
        await client.close()

    assert second.finish_reason == "stop"
    assert isinstance(second.content, str)
    assert "42" in second.content
