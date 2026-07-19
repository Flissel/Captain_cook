"""Evaluation CLI contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from autogen_core.models import CreateResult, ModelFamily, ModelInfo, RequestUsage
from autogen_ext.models.replay import ReplayChatCompletionClient

from agenten.evaluation.cli import (
    EvaluationSourceDigestMismatch,
    EvaluationCallBudgetExceeded,
    UsageTrackingChatCompletionClient,
    async_main,
    build_evaluation_model_client,
    verify_source_digest,
)
from agenten.evaluation.models import EvaluationManifest, EvaluationStatus
from agenten.evaluation.source import load_evaluation_source
from agenten.evaluation.store import JsonEvaluationStore


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
async def test_usage_tracking_client_enforces_persisted_provider_call_budget(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")
    source = load_evaluation_source(input_path, "agentfarm/input.md", 12_000)
    store = JsonEvaluationStore(tmp_path / "artifacts")
    await store.create_run(
        source,
        run_id="eval-budget",
        idempotency_key=source.sha256,
        max_calls=4,
    )
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
        store=store,
        run_id="eval-budget",
    )

    for _ in range(4):
        await client.create([])

    with pytest.raises(EvaluationCallBudgetExceeded, match="exhausted"):
        await client.create([])
    telemetry = client.evaluation_telemetry()
    assert telemetry.model_identifier == "replay-live-shape"
    assert telemetry.call_count == 4
    assert telemetry.token_total >= 0


@pytest.mark.asyncio
async def test_cli_resume_cannot_replace_persisted_four_call_budget_with_current_hundred(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")
    source = load_evaluation_source(input_path, "agentfarm/input.md", 12_000)
    output = tmp_path / "artifacts"
    store = JsonEvaluationStore(output)
    await store.create_run(
        source,
        run_id="eval-resume",
        idempotency_key=source.sha256,
        max_calls=4,
    )
    for _ in range(4):
        await store.reserve_provider_call("eval-resume", model_identifier="replay")
    inner = ReplayChatCompletionClient(
        ["must-not-run"],
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
            "eval-resume",
            "--max-calls",
            "100",
        ],
        model_client=inner,
    )

    assert exit_code == 2
    assert inner._current_index == 0
    manifest = EvaluationManifest.model_validate_json(
        (output / "eval-resume" / "run-manifest.json").read_bytes()
    )
    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.call_count == 4
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_configured_source_digest_mismatch_hard_fails(tmp_path: Path) -> None:
    source = tmp_path / "input.md"
    source.write_text("# changed\n", encoding="utf-8")

    with pytest.raises(EvaluationSourceDigestMismatch, match="digest"):
        verify_source_digest(source, expected_sha256="a" * 64)


@pytest.mark.asyncio
async def test_cli_provider_exception_returns_nonzero_with_terminal_failed_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingReplayClient(ReplayChatCompletionClient):
        async def create(self, messages: object, **kwargs: object):  # type: ignore[override]
            raise RuntimeError("provider failed with runtime-only-secret")

    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")
    output = tmp_path / "artifacts"
    client = FailingReplayClient(
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
            "eval-provider-error",
            "--max-calls",
            "4",
        ],
        model_client=client,
    )

    assert exit_code == 2
    manifest = EvaluationManifest.model_validate_json(
        (output / "eval-provider-error" / "run-manifest.json").read_bytes()
    )
    assert manifest.status is EvaluationStatus.FAILED
    assert manifest.call_count == 1
    assert manifest.token_total == 0
    assert manifest.cost_total is None
    assert (output / "eval-provider-error" / "evaluation.md").is_file()
    summary = capsys.readouterr().out
    assert "runtime-only-secret" not in summary


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
                cost_total=None,
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

    assert exit_code == 2
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


@pytest.mark.asyncio
async def test_usage_tracking_telemetry_survives_client_restart(tmp_path: Path) -> None:
    inner = ReplayChatCompletionClient(
        [
            CreateResult(
                finish_reason="stop",
                content="one",
                usage=RequestUsage(prompt_tokens=11, completion_tokens=7),
                cached=False,
            )
        ],
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family=ModelFamily.UNKNOWN,
            structured_output=True,
        ),
    )
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")
    source = load_evaluation_source(input_path, "agentfarm/input.md", 12_000)
    store = JsonEvaluationStore(tmp_path / "artifacts")
    await store.create_run(source, run_id="eval-usage", idempotency_key=source.sha256, max_calls=4)
    first = UsageTrackingChatCompletionClient(
        inner,
        model_identifier="usage-test",
        store=store,
        run_id="eval-usage",
    )

    await first.create([])
    restarted = UsageTrackingChatCompletionClient(
        ReplayChatCompletionClient(
            [],
            model_info=inner.model_info,
        ),
        model_identifier="usage-test",
        store=JsonEvaluationStore(tmp_path / "artifacts"),
        run_id="eval-usage",
    )

    telemetry = restarted.evaluation_telemetry()
    assert telemetry.call_count == 1
    assert telemetry.token_total == 18
    assert telemetry.cost_total is None
