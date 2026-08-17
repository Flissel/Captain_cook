from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from gateway.capability_tool_host_app import create_capability_tool_host_app


TOKEN = "tool-host-test-token-123"
CODE_SHA = "a" * 64


def _datasets():
    values = {
        "sales": {"schema": "captain.demo-sales-tool-dataset.v1", "dataset_kind": "demo_fixture_evidence", "records": [{"account_id": "demo-acme", "annual_value_usd": 120000}]},
        "incident": {"schema": "captain.demo-incident-tool-dataset.v1", "dataset_kind": "demo_fixture_evidence", "records": [{"alert_id": "demo-alert-17", "service": "checkout-api"}]},
        "claims": {"schema": "captain.demo-claims-tool-dataset.v1", "dataset_kind": "demo_fixture_evidence", "records": [{"claim_id": "demo-claim-42", "policy_id": "demo-policy-auto-1"}]},
    }
    digests = {
        key: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for key, value in values.items()
    }
    return values, digests


def test_tool_host_requires_auth_and_returns_content_addressed_demo_truth() -> None:
    datasets, digests = _datasets()
    app = create_capability_tool_host_app(
        bearer_token=TOKEN, datasets=datasets, dataset_digests=digests,
        implementation_sha256=CODE_SHA,
    )
    payload = {
        "schema": "captain.generic-tool-execution.v1",
        "tool_key": "alert_context_reader", "operation": "read",
        "endpoint_sha256": hashlib.sha256(b"http://127.0.0.1:8110/v1/alerts/context").hexdigest(),
        "registry_sha256": "b" * 64,
        "effect_id": "e" * 64,
        "arguments": {"alert_id": "demo-alert-17", "caller_answer": "must-not-echo"},
    }
    with TestClient(app) as client:
        assert client.post("/v1/alerts/context", json=payload).status_code == 401
        response = client.post(
            "/v1/alerts/context", json=payload,
            headers={"Authorization": "Bearer " + TOKEN},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["truth_scope"] == "demo_fixture_evidence"
    assert result["dataset_sha256"] == digests["incident"]
    assert result["implementation_sha256"] == CODE_SHA
    assert result["matches"] == [{"alert_id": "demo-alert-17", "service": "checkout-api"}]
    assert "must-not-echo" not in json.dumps(result)


def test_tool_host_rejects_route_binding_drift_before_dataset_readback() -> None:
    datasets, digests = _datasets()
    app = create_capability_tool_host_app(
        bearer_token=TOKEN, datasets=datasets, dataset_digests=digests,
        implementation_sha256=CODE_SHA,
    )
    payload = {
        "schema": "captain.generic-tool-execution.v1",
        "tool_key": "alert_context_reader", "operation": "read",
        "endpoint_sha256": "f" * 64, "registry_sha256": "b" * 64,
        "effect_id": "e" * 64,
        "arguments": {"alert_id": "demo-alert-17"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/alerts/context", json=payload,
            headers={"Authorization": "Bearer " + TOKEN},
        )
    assert response.status_code == 409


def test_tool_host_restart_replays_durable_receipt_without_recomputing(tmp_path) -> None:
    datasets, digests = _datasets()
    payload = {
        "schema": "captain.generic-tool-execution.v1",
        "tool_key": "alert_context_reader", "operation": "read",
        "endpoint_sha256": hashlib.sha256(b"http://127.0.0.1:8110/v1/alerts/context").hexdigest(),
        "registry_sha256": "b" * 64, "effect_id": "e" * 64,
        "arguments": {"alert_id": "demo-alert-17"},
    }
    headers = {"Authorization": "Bearer " + TOKEN}
    first = create_capability_tool_host_app(
        bearer_token=TOKEN, datasets=datasets, dataset_digests=digests,
        implementation_sha256=CODE_SHA, journal_root=tmp_path,
    )
    with TestClient(first) as client:
        expected = client.post("/v1/alerts/context", json=payload, headers=headers).json()
    restarted = create_capability_tool_host_app(
        bearer_token=TOKEN,
        datasets={**datasets, "incident": {**datasets["incident"], "records": []}},
        dataset_digests=digests, implementation_sha256=CODE_SHA,
        journal_root=tmp_path,
    )
    with TestClient(restarted) as client:
        replay = client.post("/v1/alerts/context", json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == expected


def test_tool_host_routes_cover_every_shipped_demo_tool() -> None:
    datasets, digests = _datasets()
    app = create_capability_tool_host_app(
        bearer_token=TOKEN, datasets=datasets, dataset_digests=digests,
        implementation_sha256=CODE_SHA,
    )
    paths = {route.path for route in app.routes}
    assert {
        "/v1/sales/accounts", "/v1/sales/opportunity-score",
        "/v1/sales/industry-signals", "/v1/alerts/context",
        "/v1/services/dependencies", "/v1/audit/query",
        "/v1/status/drafts", "/v1/policies/lookup",
        "/v1/claims/evidence", "/v1/ocr/extract",
    }.issubset(paths)
