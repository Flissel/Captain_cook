from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi.testclient import TestClient

from gateway.capability_benchmark_process_app import create_capability_benchmark_process_app
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
