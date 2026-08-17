from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from gateway.capability_benchmark_process_app import create_capability_benchmark_process_app
from gateway.capability_benchmark_executor_adapter import (
    GatewayOwnedBenchmarkExecutor,
    build_capability_benchmark_executor,
)
from tests.gateway.test_capability_benchmark_process_executor import _request, _response


class _Executor:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        payload = __import__("json").loads(_response({
            "request_nonce": str(request.request_nonce),
            "request_digest": request.request_digest,
        }))
        from gateway.capability_benchmark_process_executor import GatewayIsolatedBenchmarkExecutorClient

        return GatewayIsolatedBenchmarkExecutorClient._parse_response(
            request, __import__("json").dumps(payload).encode()
        )


def _payload() -> dict[str, object]:
    request = _request()
    return {
        "schema": "captain.capability-benchmark-execution-request.v1",
        "job_id": str(request.job_id),
        "capability_id": request.capability_id,
        "input_ref": request.input_ref.model_dump(mode="json"),
        "case": {
            "case_id": request.case_ref.case_id,
            "category": request.case_ref.category.value,
            "case_digest": request.case_ref.case_digest,
        },
        "team_kind": request.team_kind,
        "model_id": request.model_id,
        "model_policy_digest": request.model_policy_digest,
        "execution_case": {
            "setup": request.execution_case.setup,
            "action": request.execution_case.action,
            "opaque_source_refs": request.execution_case.opaque_source_refs,
            "declared_tool_keys": request.execution_case.declared_tool_keys,
            "tool_authority_sha256": request.execution_case.tool_authority_sha256,
        },
        "execution_plan": {
            "candidate_ref": request.execution_plan.candidate_ref.model_dump(mode="json"),
            "candidate_manifest_ref": request.execution_plan.candidate_manifest_ref.model_dump(mode="json"),
            "team_manifest_ref": request.execution_plan.team_manifest_ref.model_dump(mode="json"),
            "baseline_policy_ref": request.execution_plan.baseline_policy_ref.model_dump(mode="json"),
            "model_ref": request.execution_plan.model_ref.model_dump(mode="json"),
            "execution_policy_ref": request.execution_plan.execution_policy_ref.model_dump(mode="json"),
            "team_agent_names": request.execution_plan.team_agent_names,
            "baseline_agent_name": request.execution_plan.baseline_agent_name,
        },
        "request_nonce": str(request.request_nonce),
        "request_digest": request.request_digest,
    }


def test_benchmark_process_exposes_authenticated_health_and_exact_execute_contract() -> None:
    executor = _Executor()
    authority_id = UUID("166b2b95-b6d5-5737-a2f2-f377b46004b8")
    app = create_capability_benchmark_process_app(
        executor=executor,
        bearer_token="benchmark-process-token",
        authority_id=authority_id,
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 401
        health = client.get(
            "/healthz", headers={"Authorization": "Bearer benchmark-process-token"}
        )
        response = client.post(
            "/v1/execute",
            headers={"Authorization": "Bearer benchmark-process-token"},
            json=_payload(),
        )

    assert health.json() == {
        "schema": "captain.capability-benchmark-process-health.v1",
        "status": "ready",
        "authority_id": str(authority_id),
    }
    assert response.status_code == 200
    assert response.json()["schema"] == "captain.capability-benchmark-execution-response.v1"
    assert response.json()["request_digest"] == _payload()["request_digest"]
    assert len(executor.requests) == 1


def test_benchmark_process_rejects_digest_mismatch_before_executor() -> None:
    executor = _Executor()
    app = create_capability_benchmark_process_app(
        executor=executor,
        bearer_token="benchmark-process-token",
        authority_id=UUID("166b2b95-b6d5-5737-a2f2-f377b46004b8"),
    )
    payload = _payload()
    payload["request_digest"] = hashlib.sha256(b"forged").hexdigest()

    with TestClient(app) as client:
        response = client.post(
            "/v1/execute",
            headers={"Authorization": "Bearer benchmark-process-token"},
            json=payload,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "benchmark execution request is invalid"}
    assert executor.requests == []


def test_benchmark_process_module_main_runs_the_isolated_app(
    monkeypatch,
) -> None:
    import gateway.capability_benchmark_process_app as process_app

    application = object()
    observed = []
    monkeypatch.setattr(
        process_app,
        "create_capability_benchmark_process_app_from_environment",
        lambda environment: observed.append(environment) or application,
    )
    monkeypatch.setattr(
        process_app.uvicorn,
        "run",
        lambda app, **kwargs: observed.append((app, kwargs)),
    )
    monkeypatch.setenv("CAPTAIN_CAPABILITY_BENCHMARK_PORT", "18092")

    process_app.main()

    assert len(observed) == 2
    assert observed[1] == (
        application,
        {"host": "127.0.0.1", "port": 18092, "workers": 1},
    )


def test_shipped_benchmark_constructor_accepts_only_scoped_gateway_and_proxy_authority(
    tmp_path,
) -> None:
    source_root = Path(__file__).resolve().parents[2]
    root = tmp_path / "repository"
    (root / "gateway").mkdir(parents=True)
    worker = root / "gateway/capability_benchmark_host_worker.py"
    worker.write_bytes((source_root / "gateway/capability_benchmark_host_worker.py").read_bytes())
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Captain Test"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "captain@example.invalid"), cwd=root, check=True)
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "test: worker"), cwd=root, check=True)
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    configuration = {
        "gateway_url": "http://127.0.0.1:8080",
        "gateway_token": "benchmark-gateway-token",
        "executor_authority_id": "166b2b95-b6d5-5737-a2f2-f377b46004b8",
        "repository_root": str(root), "repository_revision": revision,
        "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
        "proxy_url": "http://127.0.0.1:8091",
        "proxy_token": "benchmark-proxy-client-token",
        "context_sha256": "1" * 64, "policy_sha256": "2" * 64,
        "price_card_sha256": "3" * 64,
        "proxy_executable_sha256": "4" * 64,
        "proxy_code_sha256": "5" * 64,
        "paid_command_sha256": "6" * 64,
        "hard_ceiling_enforced": "true",
        "pricing_json": json.dumps({
            "input_cost_per_million_usd": "2.00",
            "output_cost_per_million_usd": "12.00",
        }),
    }
    executor = build_capability_benchmark_executor(configuration)

    assert type(executor) is GatewayOwnedBenchmarkExecutor
    with pytest.raises(ValueError, match="incomplete"):
        build_capability_benchmark_executor({
            **configuration, "OPENAI_API_KEY": "must-not-cross-process-boundary",
        })


def test_benchmark_constructor_attests_fresh_runtime_despite_parent_module_poison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import ModuleType

    root = Path(__file__).resolve().parents[2]
    worker = root / "gateway/capability_benchmark_host_worker.py"
    revision = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    poisoned = ModuleType("autogen_agentchat")
    poisoned.__file__ = "attacker.py"
    monkeypatch.setitem(sys.modules, "autogen_agentchat", poisoned)

    executor = GatewayOwnedBenchmarkExecutor(
        gateway_url="http://127.0.0.1:8088",
        gateway_token="benchmark-gateway-token",
        authority_id=UUID("166b2b95-b6d5-5737-a2f2-f377b46004b8"),
        repository_root=root,
        worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
        repository_revision=revision, proxy_configuration={},
    )

    assert type(executor) is GatewayOwnedBenchmarkExecutor
