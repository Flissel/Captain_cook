from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import pytest

from agenten.agent_factory.contracts import (
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
)
from agenten.agent_factory.release_gate import FactoryReleaseDecision


pytestmark = pytest.mark.live


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dumped
    pytest.fail("factory live entrypoint must return a mapping or Pydantic model")


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    assert isinstance(value, str) and value.strip(), f"missing {key}"
    return value


def _exact_usd(value: object, label: str) -> Decimal:
    assert isinstance(value, str), f"{label} must be a JSON decimal string"
    assert re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]+", value), (
        f"{label} must use non-negative plain decimal notation"
    )
    try:
        amount = Decimal(value)
    except InvalidOperation:
        pytest.fail(f"{label} is not an exact decimal receipt")
    assert amount.is_finite() and amount >= 0, f"{label} must be finite and non-negative"
    return amount


def _exact_model_payload(value: object, model_type: type[Any]) -> Any:
    assert isinstance(value, Mapping), "Gateway evidence must be structured JSON"
    raw = dict(value)
    validated = model_type.model_validate(raw)
    assert raw == validated.model_dump(mode="json", by_alias=True), (
        "Gateway evidence must contain the complete canonical contract"
    )
    return validated


def _gateway_promotion(
    value: object,
    report_binding: Mapping[str, Any],
) -> None:
    assert isinstance(value, Mapping), "release requires a structured Gateway promotion"
    assert set(value) == {"projection_status", "release_decision", "promotion_block"}
    assert value.get("projection_status") == "ready_to_use"

    decision = _exact_model_payload(
        value.get("release_decision"),
        FactoryReleaseDecision,
    )
    promotion_block = _exact_model_payload(
        value.get("promotion_block"),
        FactoryEvidenceBlock,
    )
    assert decision.status == "ready"
    assert promotion_block.phase is FactoryPhase.CAPABILITY_PROMOTED
    assert promotion_block.producer == "captain"
    assert promotion_block.status is FactoryBlockStatus.SUCCEEDED
    assert promotion_block.evidence_refs
    assert decision.evaluation_id is not None
    assert decision.evaluation_ref is not None
    assert decision.evaluation_ref in promotion_block.artifact_refs

    job_id = _required_text(report_binding, "job_id")
    correlation_id = _required_text(report_binding, "correlation_id")
    subject_version = report_binding.get("subject_version")
    attempt = report_binding.get("attempt")
    assert isinstance(subject_version, int) and not isinstance(subject_version, bool)
    assert isinstance(attempt, int) and not isinstance(attempt, bool)
    assert str(decision.job_id) == job_id
    assert str(decision.correlation_id) == correlation_id
    assert str(promotion_block.job_id) == job_id
    assert str(promotion_block.correlation_id) == correlation_id
    assert promotion_block.subject_version == subject_version
    assert promotion_block.attempt == attempt


def _assert_redacted(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if (
                normalized_key in {
                    "api_key",
                    "access_token",
                    "authorization",
                    "password",
                    "secret",
                    "token",
                    "raw_prompt",
                    "private",
                    "path",
                }
                or normalized_key.startswith("private_")
                or normalized_key.endswith("_path")
            ):
                raise AssertionError("live report contains a forbidden sensitive field")
            _assert_redacted(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_redacted(nested)
    elif isinstance(value, str):
        normalized = value.replace("\\", "/")
        if re.search(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", value):
            raise AssertionError("live report contains bearer authorization material")
        if re.search(
            r"(?i)(?<![a-z])[a-z]:/|(?<!:)//|/(?:home|Users|tmp|var|etc)/",
            normalized,
        ):
            raise AssertionError("live report contains an absolute host path")


async def _run_sanitized_live_gate(
    runner: Callable[[], Awaitable[object]],
) -> object:
    failure_message: str | None = None
    result: object = None
    try:
        result = await runner()
    except BaseException as error:
        if isinstance(error, pytest.skip.Exception):
            failure_message = "the live runtime may not skip after prerequisites are confirmed"
        else:
            failure_message = "the live runtime failed after prerequisites were confirmed"
    if failure_message is not None:
        pytest.fail(failure_message, pytrace=False)
    return result


@pytest.mark.asyncio
async def test_hermes_six_skill_factory_live() -> None:
    if os.environ.get("CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED") != "1":
        pytest.skip("run scripts/run-hermes-factory-live-gate.ps1 to confirm prerequisites")

    try:
        entrypoint = importlib.import_module(
            "agenten.agent_factory.factory_live_entrypoint"
        )
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"factory live runtime merge contract is unavailable: {type(error).__name__}")
    runner = getattr(entrypoint, "run_factory_live_gate_from_environment", None)
    if not callable(runner):
        pytest.fail("factory live runtime entrypoint is missing the agreed async runner")

    result = await _run_sanitized_live_gate(runner)
    report = _mapping(result)
    _assert_redacted(report)

    assert report.get("schema") == "captain.hermes-six-skill-factory-live-report.v1"
    mode = os.environ["CAPTAIN_FACTORY_GATE_MODE"]
    assert report.get("mode") == mode
    assert report.get("prerequisites_confirmed") is True
    assert report.get("live_execution") is True
    assert report.get("model") == os.environ["CAPTAIN_FACTORY_MODEL"]
    assert report.get("database_name") == "captain_test"
    _required_text(report, "context7_provenance_digest")

    provider_traces = report.get("provider_traces")
    expected_run_count = 1 if mode == "demo" else 3
    assert isinstance(provider_traces, list)
    assert len(provider_traces) == expected_run_count
    trace_ids: list[str] = []
    codex_session_ids: list[str] = []
    total_cost = Decimal("0")
    for raw_trace in provider_traces:
        trace = _mapping(raw_trace)
        trace_ids.append(_required_text(trace, "trace_id"))
        codex_session_ids.append(_required_text(trace, "codex_session_id"))
        _required_text(trace, "provider")
        assert trace.get("model") == os.environ["CAPTAIN_FACTORY_MODEL"]
        assert trace.get("status") == "succeeded"
        cost = _exact_usd(trace.get("cost_usd"), "provider trace cost_usd")
        total_cost += cost
    assert len(trace_ids) == len(set(trace_ids))
    assert len(codex_session_ids) == len(set(codex_session_ids))
    assert total_cost == _exact_usd(report.get("total_cost_usd"), "total_cost_usd")
    assert total_cost <= Decimal(os.environ["CAPTAIN_FACTORY_MAX_COST_USD"])

    if mode == "demo":
        assert report.get("terminal_status") == "demo_ready"
        assert report.get("terminal_status") != "ready_to_use"
        assert report.get("recovery") in (None, {})
        assert report.get("gateway_promotion") in (None, {})
    else:
        recovery = _mapping(report.get("recovery"))
        assert recovery.get("status") == "recovered"
        _required_text(recovery, "evidence_digest")
        _gateway_promotion(report.get("gateway_promotion"), report)
        assert report.get("terminal_status") == "ready_to_use"

    with_n8n = os.environ.get("CAPTAIN_FACTORY_WITH_N8N") == "1"
    assert report.get("with_n8n") is with_n8n
    if with_n8n:
        n8n_evidence = _mapping(report.get("n8n_evidence"))
        assert re.fullmatch(
            r"^[0-9a-f]{64}$",
            _required_text(n8n_evidence, "workflow_digest"),
        )
        _required_text(n8n_evidence, "n8n_mcp_call_id")
        _required_text(n8n_evidence, "n8n_execution_id")

    report_directory = Path(os.environ["CAPTAIN_FACTORY_REPORT_DIRECTORY"]).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    assert not report_directory.is_relative_to(repository_root)
    reports = tuple(report_directory.glob("sha256-*.json"))
    assert len(reports) == 1
    report_path = reports[0]
    report_bytes = report_path.read_bytes()
    digest = hashlib.sha256(report_bytes).hexdigest()
    assert report_path.name == f"sha256-{digest}.json"
    persisted = json.loads(report_bytes.decode("utf-8"))
    _assert_redacted(persisted)
    assert persisted == report
