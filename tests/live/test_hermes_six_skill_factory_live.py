from __future__ import annotations

import hashlib
import importlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import pytest


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


def _assert_redacted(value: object) -> None:
    forbidden_keys = {
        "api_key",
        "authorization",
        "password",
        "secret",
        "token",
    }
    if isinstance(value, Mapping):
        assert not ({str(key).lower() for key in value} & forbidden_keys)
        for nested in value.values():
            _assert_redacted(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_redacted(nested)
    elif isinstance(value, str):
        normalized = value.replace("\\", "/")
        assert ":/Users/" not in normalized
        assert not normalized.startswith(("/home/", "/Users/"))


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

    try:
        result = await runner()
    except pytest.skip.Exception:
        pytest.fail("the live runtime may not skip after prerequisites are confirmed")
    report = _mapping(result)

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
    total_cost = Decimal("0")
    for raw_trace in provider_traces:
        trace = _mapping(raw_trace)
        trace_ids.append(_required_text(trace, "trace_id"))
        _required_text(trace, "codex_session_id")
        _required_text(trace, "provider")
        assert trace.get("model") == os.environ["CAPTAIN_FACTORY_MODEL"]
        assert trace.get("status") == "succeeded"
        try:
            cost = Decimal(str(trace["cost_usd"]))
        except (InvalidOperation, KeyError) as error:
            pytest.fail(f"provider trace has no exact USD receipt: {type(error).__name__}")
        assert cost.is_finite() and cost >= 0
        total_cost += cost
    assert len(trace_ids) == len(set(trace_ids))
    assert total_cost == Decimal(str(report.get("total_cost_usd")))
    assert total_cost <= Decimal(os.environ["CAPTAIN_FACTORY_MAX_COST_USD"])

    if mode == "demo":
        assert report.get("terminal_status") == "demo_ready"
        assert report.get("terminal_status") != "ready_to_use"
        assert report.get("recovery") in (None, {})
    else:
        recovery = _mapping(report.get("recovery"))
        assert recovery.get("status") == "recovered"
        _required_text(recovery, "evidence_digest")
        assert report.get("terminal_status") == "ready_to_use"

    with_n8n = os.environ.get("CAPTAIN_FACTORY_WITH_N8N") == "1"
    assert report.get("with_n8n") is with_n8n
    if with_n8n:
        n8n_evidence = _mapping(report.get("n8n_evidence"))
        assert len(_required_text(n8n_evidence, "workflow_digest")) == 64
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
    assert persisted == report
    _assert_redacted(persisted)
