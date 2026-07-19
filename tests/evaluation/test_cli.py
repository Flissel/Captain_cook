from __future__ import annotations

import json
from pathlib import Path

import pytest
from autogen_core.models import ModelFamily, ModelInfo, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.evaluation.cli import (
    EvaluationCallBudgetExceeded,
    UsageTrackingChatCompletionClient,
    async_main,
    build_evaluation_model_client,
)
from agenten.evaluation.models import EvaluationManifest, EvaluationStatus


@pytest.mark.asyncio
async def test_cli_requires_a_safe_logical_source_reference(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")

    assert await async_main([str(input_path), "--source-reference", "../unsafe"]) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary == {"error": "invalid_source", "status": "failed"}
    assert str(input_path) not in json.dumps(summary)


def test_evaluation_model_client_disables_parallel_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        "agenten.evaluation.cli.OpenAIChatCompletionClient",
        CapturingClient,
    )

    client = build_evaluation_model_client(
        model="gpt-5.6",
        api_key="runtime-only-secret",
    )

    assert isinstance(client, CapturingClient)
    assert captured["parallel_tool_calls"] is False
    assert captured["reasoning_effort"] == "none"
    assert captured["max_retries"] == 0
    assert captured["model"] == "gpt-5.6"
    assert captured["api_key"] == "runtime-only-secret"
    assert captured["model_info"] == ModelInfo(
        vision=True,
        function_calling=True,
        json_output=True,
        family=ModelFamily.GPT_5,
        structured_output=True,
    )


@pytest.mark.asyncio
async def test_usage_tracking_client_enforces_provider_call_budget() -> None:
    inner = ReplayChatCompletionClient(
        ["one", "two", "three", "four", "must-not-run"],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
    client = UsageTrackingChatCompletionClient(
        inner,
        model_identifier="replay-live-shape",
        max_calls=4,
    )

    for _ in range(4):
        await client.create([])

    with pytest.raises(EvaluationCallBudgetExceeded, match="four calls"):
        await client.create([])
    telemetry = client.evaluation_telemetry()
    assert telemetry.model_identifier == "replay-live-shape"
    assert telemetry.call_count == 4
    assert telemetry.token_total >= 0


@pytest.mark.asyncio
async def test_cli_emits_only_safe_relative_evidence_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "private" / "input.md"
    input_path.parent.mkdir()
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")
    output = tmp_path / "private-output"
    captured: dict[str, object] = {}

    class FakeService:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def run(self, run_id: str) -> EvaluationManifest:
            source = captured["source"]
            return EvaluationManifest(
                run_id=run_id,
                idempotency_key="source-digest",
                status=EvaluationStatus.FAILED,
                source=source,
                component_outcomes=(),
                model_identifier="replay-live-shape",
                prompt_version="agentfarm-evaluation-v1",
                call_count=4,
                token_total=12,
                cost_total=0.0,
                artifact_digests=(),
            )

    monkeypatch.setattr("agenten.evaluation.cli.AgentFarmEvaluationService", FakeService)
    client = ReplayChatCompletionClient(
        [],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )

    exit_code = await async_main(
        [
            str(input_path),
            "--source-reference",
            "agentfarm/input.md",
            "--output",
            str(output),
            "--run-id",
            "eval-live-001",
            "--model",
            "replay-live-shape",
            "--max-components",
            "1",
            "--max-rounds",
            "1",
            "--max-calls",
            "4",
        ],
        model_client=client,
    )

    assert exit_code == 0
    assert captured["max_components"] == 1
    assert captured["max_rounds"] == 1
    assert captured["max_calls"] == 4
    assert callable(captured["telemetry"])
    assert isinstance(captured["summary_model_client"], ReplayChatCompletionClient)
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "artifact_reference": "eval-live-001/evaluation.md",
        "call_count": 4,
        "model_identifier": "replay-live-shape",
        "run_id": "eval-live-001",
        "status": "failed",
        "token_total": 12,
    }
    serialized = json.dumps(summary)
    assert str(input_path) not in serialized
    assert str(output) not in serialized


def test_usage_tracking_telemetry_uses_underlying_token_totals() -> None:
    class UsageOnlyReplay(ReplayChatCompletionClient):
        def total_usage(self) -> RequestUsage:
            return RequestUsage(prompt_tokens=11, completion_tokens=7)

    inner = UsageOnlyReplay(
        [],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
    client = UsageTrackingChatCompletionClient(
        inner,
        model_identifier="usage-test",
        max_calls=4,
    )

    assert client.evaluation_telemetry().token_total == 18
