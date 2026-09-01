"""Tests for the LLM-backed `HouseholderExecutor` implementation.

`agenten.household.executor.DeterministicHouseholderExecutor` stays the
offline default used by the rest of the suite -- this file drives the second,
optional implementation (`agenten.household.llm_executor.LlmHouseholderExecutor`)
entirely through a hand-rolled fake `ChatCompletionClient`. No network call,
no real model: mirrors the constructor-injection seam
`agenten.llm.judge.make_llm_judge` and `tests/llm/test_judge.py` already use
for the real judge adapter.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Sequence

import pytest

from agenten.household.executor import (
    DeterministicHouseholderExecutor,
    HouseholderExecutionError,
    HouseholderReport,
)
from agenten.household.llm_executor import HouseholderReportModel, LlmHouseholderExecutor
from agenten.household.roles import load_householder_roles


class _CreateResult:
    """Minimal stand-in for `autogen_core.models.CreateResult`."""

    def __init__(self, content: Any) -> None:
        self.content = content


class FakeChatCompletionClient:
    """Fake `ChatCompletionClient`: no network, no real model.

    Replays canned string responses (or raises canned exceptions) from
    `responses`, one per `create()` call, and records every `messages`
    argument it was invoked with so tests can assert on prompt content.
    """

    def __init__(self, responses: Sequence[Any] = ()) -> None:
        self._responses = list(responses)
        self.calls: List[Sequence[Any]] = []

    async def create(self, messages, **kwargs):
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("FakeChatCompletionClient called more times than responses configured")
        next_response = self._responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return _CreateResult(next_response)


class HangingChatCompletionClient:
    """Fake `ChatCompletionClient` whose `create()` never resolves.

    Used to exercise a genuine `asyncio.wait_for` timeout inside
    `agenten.llm.resilience.run_llm_stage`, the same way
    `tests/llm/test_resilience.py::test_stage_timeout_cleans_up_each_attempt_and_preserves_cause`
    does for the Captain planning stages.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, messages, **kwargs):
        self.calls += 1
        await asyncio.Event().wait()


def _architect_role():
    return load_householder_roles()[0]


def _report_json(**overrides: Any) -> str:
    fields = dict(
        decision="delivery_plan_approved",
        artifacts=["agents/household/architect.md"],
        evidence=["reviewed the batch schema"],
        limitations=["no live deployment was attempted"],
        tools_used=["Read"],
    )
    fields.update(overrides)
    return HouseholderReportModel(**fields).model_dump_json()


@pytest.mark.asyncio
async def test_well_formed_response_maps_to_a_householder_report():
    role = _architect_role()
    client = FakeChatCompletionClient([_report_json()])
    executor = LlmHouseholderExecutor(client)

    report = await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert isinstance(report, HouseholderReport)
    assert report.role == role.role_id
    assert report.decision == "delivery_plan_approved"
    assert report.artifacts == ("agents/household/architect.md",)
    assert "reviewed the batch schema" in report.evidence
    assert report.limitations == ("no live deployment was attempted",)


@pytest.mark.asyncio
async def test_role_prompt_markdown_is_read_and_reaches_the_messages():
    role = _architect_role()
    client = FakeChatCompletionClient([_report_json()])
    executor = LlmHouseholderExecutor(client)

    await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert len(client.calls) == 1
    sent_text = "\n".join(str(getattr(message, "content", "")) for message in client.calls[0])
    prompt_text = role.prompt_path.read_text(encoding="utf-8")
    # A distinctive sentence from agents/household/architect.md, proving the
    # actual file content (not just its path) reached the model messages.
    assert "You protect system boundaries" in prompt_text
    assert "You protect system boundaries" in sent_text


@pytest.mark.asyncio
async def test_permitted_tools_reach_the_prompt():
    role = _architect_role()
    assert role.permitted_tools == ("Read", "Write", "Grep", "Bash")
    client = FakeChatCompletionClient([_report_json()])
    executor = LlmHouseholderExecutor(client)

    await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    sent_text = "\n".join(str(getattr(message, "content", "")) for message in client.calls[0])
    for tool in role.permitted_tools:
        assert tool in sent_text


@pytest.mark.asyncio
async def test_claiming_an_unpermitted_tool_raises_retriable():
    role = _architect_role()
    client = FakeChatCompletionClient([_report_json(tools_used=["Bash", "Deploy"])])
    executor = LlmHouseholderExecutor(client)

    with pytest.raises(HouseholderExecutionError) as failure:
        await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert failure.value.retriable is True
    assert "Deploy" in str(failure.value)


@pytest.mark.asyncio
async def test_malformed_json_response_raises_retriable():
    role = _architect_role()
    client = FakeChatCompletionClient(["not valid json"])
    executor = LlmHouseholderExecutor(client)

    with pytest.raises(HouseholderExecutionError) as failure:
        await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert failure.value.retriable is True


@pytest.mark.asyncio
async def test_timeout_raises_retriable():
    role = _architect_role()
    client = HangingChatCompletionClient()
    executor = LlmHouseholderExecutor(client, timeout_seconds=0.01, max_attempts=1)

    with pytest.raises(HouseholderExecutionError) as failure:
        await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert failure.value.retriable is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_empty_subproblem_id_raises_not_retriable_before_any_model_call():
    role = _architect_role()
    client = FakeChatCompletionClient([_report_json()])
    executor = LlmHouseholderExecutor(client)

    with pytest.raises(HouseholderExecutionError) as failure:
        await executor.run(role, "", "Review the planner/ledger boundary")

    assert failure.value.retriable is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_empty_description_raises_not_retriable_before_any_model_call():
    role = _architect_role()
    client = FakeChatCompletionClient([_report_json()])
    executor = LlmHouseholderExecutor(client)

    with pytest.raises(HouseholderExecutionError) as failure:
        await executor.run(role, "sp-1", "   ")

    assert failure.value.retriable is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_deterministic_executor_still_satisfies_the_port_unchanged():
    """`DeterministicHouseholderExecutor` stays the offline default; this is
    a light regression check that it is untouched by this file's changes."""
    role = _architect_role()
    executor = DeterministicHouseholderExecutor()

    report = await executor.run(role, "sp-1", "Review the planner/ledger boundary")

    assert report == HouseholderReport(
        role="architect",
        decision="offline_review_completed",
        artifacts=("agents/household/architect.md",),
        evidence=("deterministic offline executor", "subproblem:sp-1"),
        limitations=("No LLM, MCP server, browser, or deployment was invoked.",),
    )
