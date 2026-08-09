from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import urllib.error
from uuid import UUID

import pytest

from agenten.demo_orchestrator.client import (
    CapabilityDemoClient,
    DemoClientError,
    DemoTimeout,
    UrllibJsonHttp,
)
from agenten.demo_orchestrator.processes import DemoProcessTimeout
from tests.demo_orchestrator.test_processes import _configuration


class FakeSupervisor:
    def __init__(self) -> None:
        self.restarts = 0

    def restart_runtime(self, *, timeout_seconds: float) -> object:
        assert timeout_seconds > 0
        self.restarts += 1
        return object()


class FakeAssemblyHttp:
    def __init__(self, *, timeout_once_at: int | None = None, mismatch: bool = False) -> None:
        self.calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.assembly_id: str | None = None
        self.request_sha256 = "d" * 64
        self.completed = 0
        self.timeout_once_at = timeout_once_at
        self.timed_out = False
        self.mismatch = mismatch

    def request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        payload: dict[str, Any] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        assert timeout_seconds > 0
        self.calls.append((method, url, token, payload))
        if (
            self.timeout_once_at is not None
            and self.completed == self.timeout_once_at
            and method == "POST"
            and url.endswith("/advances")
            and not self.timed_out
        ):
            self.timed_out = True
            raise TimeoutError("controlled fake timeout")
        if method == "POST" and url.endswith("/v1/capability-assemblies"):
            assert payload is not None
            self.assembly_id = payload["assembly_id"]
            return self._checkpoint()
        if method == "POST" and url.endswith("/advances"):
            assert payload is not None
            start = self.completed
            self.completed += 1
            return {
                "schema": "captain.capability-assembly-advance-receipt.v1",
                "assembly_id": self.assembly_id,
                "operation_id": payload["operation_id"],
                "request_sha256": self.request_sha256,
                "maximum_slices": 1,
                "start_completed_slices": start,
                "end_completed_slices": self.completed,
                "executed_slices": 1,
                "status": "ready_to_use" if self.completed == 24 else "advancing",
            }
        if method == "GET" and url.endswith("/final-evidence"):
            return self._evidence()
        if method == "GET" and "/v1/projections/" in url:
            evidence = self._evidence()
            if self.mismatch:
                evidence["booked_cost_usd"] = "5.99"
            return evidence
        if method == "GET" and self.assembly_id and url.endswith(self.assembly_id):
            return self._checkpoint()
        raise AssertionError(f"unexpected HTTP request: {method} {url}")

    def _checkpoint(self) -> dict[str, Any]:
        kinds = (
            ["scaffold_and_test_harness", "codex_implementation", "seal", "evaluate", "promote", "execute", "project"]
            * 3
            + ["resume_authorize", "resume_dispatch", "resume_readback"]
        )
        last = None
        if self.completed:
            ordinal = self.completed - 1
            last = {"ordinal": ordinal, "kind": kinds[ordinal], "input_id": None}
        return {
            "schema": "captain.capability-assembly-checkpoint.v1",
            "assembly_id": self.assembly_id,
            "request_sha256": self.request_sha256,
            "revision": self.completed + 1,
            "status": "ready_to_use" if self.completed == 24 else "advancing",
            "completed_slices": self.completed,
            "last_slice": last,
        }

    def _evidence(self) -> dict[str, Any]:
        return {
            "schema": "captain.capability-live-demo-evidence.v1",
            "run_id": self.assembly_id,
            "status": "ready_to_use",
            "booked_cost_usd": "6.00",
            "correlation_ids": ["captain-issued-correlation-a", "captain-issued-correlation-b"],
        }


def test_paid_gate_and_six_dollar_ceiling_stop_before_http(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    http = FakeAssemblyHttp()
    client = CapabilityDemoClient(
        configuration,
        supervisor=FakeSupervisor(),
        http=http,
        reporter=lambda _: None,
    )

    with pytest.raises(DemoClientError, match="explicit paid-run authorization"):
        client.run(allow_paid_run=False, maximum_cost_usd="6.00", timeout_seconds=5)
    with pytest.raises(DemoClientError, match="USD 6.00"):
        client.run(allow_paid_run=True, maximum_cost_usd="6.01", timeout_seconds=5)

    assert http.calls == []


def test_run_posts_canonical_inputs_advances_single_slices_restarts_and_verifies(
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    http = FakeAssemblyHttp()
    supervisor = FakeSupervisor()
    progress: list[dict[str, object]] = []
    client = CapabilityDemoClient(
        configuration,
        supervisor=supervisor,
        http=http,
        reporter=progress.append,
    )

    evidence = client.run(
        allow_paid_run=True,
        maximum_cost_usd="6.00",
        timeout_seconds=30,
    )

    submit = http.calls[0][3]
    assert submit is not None
    assert [item["repository_path"] for item in submit["inputs"]] == [
        "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
        "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
        "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
    ]
    assert all("pattern" not in item for item in submit["inputs"])
    assert [item["reservation"]["maximum_cost_usd"] for item in submit["inputs"]] == [
        "2.00",
        "2.00",
        "2.00",
    ]
    advances = [call for call in http.calls if call[1].endswith("/advances")]
    assert len(advances) == 24
    assert all(call[3] is not None and call[3]["maximum_slices"] == 1 for call in advances)
    assert supervisor.restarts == 1
    assert evidence["status"] == "ready_to_use"
    assert all(set(event) <= {"schema", "status", "completed_slices", "service", "assembly_id"} for event in progress)
    assert "secret" not in json.dumps(progress)


def test_timeout_is_persisted_as_124_and_resume_keeps_same_assembly(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    http = FakeAssemblyHttp(timeout_once_at=3)
    supervisor = FakeSupervisor()
    client = CapabilityDemoClient(
        configuration,
        supervisor=supervisor,
        http=http,
        reporter=lambda _: None,
    )

    with pytest.raises(DemoTimeout) as error:
        client.run(allow_paid_run=True, maximum_cost_usd="6.00", timeout_seconds=30)
    assert error.value.exit_code == 124
    state_path = configuration.state_root / "current-run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    original_assembly = state["assembly_id"]
    assert state["last_exit_code"] == 124

    evidence = client.resume(
        allow_paid_run=True,
        maximum_cost_usd="6.00",
        timeout_seconds=30,
    )

    assert evidence["run_id"] == original_assembly
    submitted_ids = {
        call[3]["assembly_id"]
        for call in http.calls
        if call[0] == "POST" and call[1].endswith("/v1/capability-assemblies") and call[3]
    }
    assert submitted_ids == {original_assembly}
    assert supervisor.restarts == 1


def test_gateway_and_minibook_evidence_mismatch_fails_closed(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    client = CapabilityDemoClient(
        configuration,
        supervisor=FakeSupervisor(),
        http=FakeAssemblyHttp(mismatch=True),
        reporter=lambda _: None,
    )

    with pytest.raises(DemoClientError, match="Minibook evidence does not equal Gateway"):
        client.run(allow_paid_run=True, maximum_cost_usd="6.00", timeout_seconds=30)


def test_real_http_adapter_preserves_wrapped_socket_timeout_as_timeout_124(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_: object, **__: object) -> object:
        raise urllib.error.URLError(TimeoutError("socket timed out"))

    monkeypatch.setattr("urllib.request.urlopen", timeout)

    with pytest.raises(TimeoutError):
        UrllibJsonHttp().request_json(
            "GET",
            "http://127.0.0.1:19000/health",
            token="redacted-token",
            payload=None,
            timeout_seconds=1,
        )


def test_runtime_restart_timeout_is_persisted_as_terminal_124(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    http = FakeAssemblyHttp()

    class TimeoutSupervisor(FakeSupervisor):
        def restart_runtime(self, *, timeout_seconds: float) -> object:
            raise DemoProcessTimeout("Runtime restart timed out")

    client = CapabilityDemoClient(
        configuration,
        supervisor=TimeoutSupervisor(),
        http=http,
        reporter=lambda _: None,
    )

    with pytest.raises(DemoTimeout) as error:
        client.run(allow_paid_run=True, maximum_cost_usd="6.00", timeout_seconds=30)

    assert error.value.exit_code == 124
    state = json.loads(
        (configuration.state_root / "current-run.json").read_text(encoding="utf-8")
    )
    assert state["last_exit_code"] == 124
