from __future__ import annotations

import asyncio

import pytest

from minibook.swarm import llm


@pytest.mark.asyncio
async def test_openai_text_times_out_without_leaving_provider_call_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_completion(**kwargs: object) -> object:
        del kwargs
        await asyncio.sleep(1)
        raise AssertionError("timeout should cancel this completion")

    monkeypatch.setenv("MINIBOOK_LLM_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(llm, "_budgeted_openai_chat_completion", slow_completion)

    assert await llm._openai_text("system", "user", 10) == (
        "[OpenAI Error: provider request timed out]"
    )


def test_provider_timeout_rejects_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIBOOK_LLM_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="positive number"):
        llm._provider_timeout_seconds()
