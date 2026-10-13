from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import httpx

from gateway.capability_benchmark_tools import (
    GenericToolAdapterDescriptor,
    GenericToolRegistryManifest,
    GenericToolRegistryConfigurationError,
    VerifiedGenericToolRegistry,
    load_verified_generic_tool_registry,
)


def _tracked_registry(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    import subprocess

    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.name", "Captain Test"), cwd=root, check=True)
    subprocess.run(("git", "config", "user.email", "captain@example.invalid"), cwd=root, check=True)
    adapter = root / "readonly_adapter.py"
    adapter.write_text(
        "def execute_generic_tool(request):\n"
        "    return {'schema': 'captain.test-tool-result.v1', "
        "'record_ref': request['arguments']['record_ref']}\n",
        encoding="utf-8",
    )
    adapter_sha = hashlib.sha256(adapter.read_bytes()).hexdigest()
    registry = root / "generic-tools.json"
    registry.write_text(json.dumps({
        "schema": "captain.generic-tool-registry.v1",
        "entries": [{
            "tool_key": "account_snapshot_reader",
            "adapter_kind": "local_readonly",
            "schema_name": "captain.test-tool-result",
            "schema_version": 1,
            "allowed_operation": "read",
            "credential_env_name": None,
            "endpoint": None,
            "endpoint_sha256": None,
            "implementation_path": "readonly_adapter.py",
            "implementation_sha256": adapter_sha,
        }],
    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "test: registry"), cwd=root, check=True)
    return root, hashlib.sha256(registry.read_bytes()).hexdigest()


def test_shipped_registry_covers_every_declared_tool_for_three_demo_inputs() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "demo_inputs/agent_factory/manifest.json").read_text())
    registry = GenericToolRegistryManifest.model_validate_json(
        (root / "config/generic_tool_registry.json").read_bytes()
    )
    available = {item.tool_key for item in registry.entries}
    for item in manifest["inputs"][:3]:
        assert set(item["required_tools"] + item["optional_tools"]) <= available


def test_shipped_registry_head_attests_all_three_demo_tool_sets() -> None:
    root = Path(__file__).resolve().parents[2]
    registry = load_verified_generic_tool_registry(
        repository_root=root,
        relative_path="config/generic_tool_registry.json",
        expected_sha256=(
            "1cc99b7ef95ddedebc64f2b1c4ad3b88c5fcbfa748f536e93606af76c1994f5f"
        ),
        environment={
            "CAPTAIN_CAPABILITY_TOOL_HOST_TOKEN": "capability-tool-host-token-123",
        },
    )
    assert registry.tool_keys == (
        "account_snapshot_reader", "alert_context_reader", "audit_log_query",
        "claim_evidence_reader", "industry_signal_reader", "ocr_extractor",
        "opportunity_scoring_rules", "policy_lookup", "service_dependency_map",
        "status_page_draft",
    )


def test_remote_registry_executes_exact_authenticated_typed_readback(monkeypatch) -> None:
    endpoint = "http://127.0.0.1:8110/v1/alerts/context"
    endpoint_sha256 = hashlib.sha256(endpoint.encode()).hexdigest()
    entry = GenericToolAdapterDescriptor(
        tool_key="alert_context_reader", adapter_kind="http_readonly",
        schema_name="captain.incident-alert-context", schema_version=1,
        allowed_operation="read",
        credential_env_name="CAPTAIN_CAPABILITY_TOOL_HOST_TOKEN",
        endpoint=endpoint, endpoint_sha256=endpoint_sha256,
        implementation_path=None, implementation_sha256=None,
        host_implementation_path="gateway/capability_tool_host_app.py",
        host_implementation_sha256="c" * 64,
        dataset_path="config/demo_tool_data/incident.json",
        dataset_sha256="d" * 64,
    )
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        payload = json.loads(request.content)
        assert payload["effect_id"] == "e" * 64
        return httpx.Response(200, json={
            "schema": "captain.incident-alert-context.v1",
            "tool_key": "alert_context_reader", "operation": "read",
            "endpoint_sha256": endpoint_sha256,
            "result": {
                "alert_refs": ["alert://redacted/1"],
                "implementation_sha256": "c" * 64,
                "dataset_sha256": "d" * 64,
                "truth_scope": "demo_fixture_evidence",
            },
        })

    real_client = httpx.Client
    monkeypatch.setattr(
        "gateway.capability_benchmark_tools.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    registry = VerifiedGenericToolRegistry(
        digest="a" * 64,
        manifest=GenericToolRegistryManifest(
            schema="captain.generic-tool-registry.v1", entries=(entry,),
        ), modules={},
        credentials={"alert_context_reader": "incident-tool-token-123"},
    )
    value = registry.execute_effect(
        tool_key="alert_context_reader", operation="read",
        arguments={"alert_ref": "alert://redacted/1"},
        effect_id="e" * 64,
    )
    assert value["result"]["alert_refs"] == ["alert://redacted/1"]
    assert len(observed) == 1
    assert observed[0].headers["Authorization"] == "Bearer incident-tool-token-123"
    assert observed[0].url == endpoint


def test_registry_loads_head_attested_adapter_and_executes_exact_schema(tmp_path: Path) -> None:
    root, digest = _tracked_registry(tmp_path)
    registry = load_verified_generic_tool_registry(
        repository_root=root, relative_path="generic-tools.json",
        expected_sha256=digest, environment={},
    )

    result = registry.execute(
        tool_key="account_snapshot_reader", operation="read",
        arguments={"record_ref": "record-1"},
    )

    assert result == {
        "schema": "captain.test-tool-result.v1", "record_ref": "record-1"
    }
    assert registry.digest == digest


@pytest.mark.parametrize("fault", ("unknown", "dirty", "credential", "endpoint"))
def test_registry_fails_closed_before_adapter_effect(tmp_path: Path, fault: str) -> None:
    root, digest = _tracked_registry(tmp_path)
    environment: dict[str, str] = {}
    if fault == "dirty":
        path = root / "readonly_adapter.py"
        path.write_text(path.read_text().replace("record_ref", "record_xef"), encoding="utf-8")
    elif fault in {"credential", "endpoint"}:
        payload = json.loads((root / "generic-tools.json").read_text())
        payload["entries"][0].update({
            "adapter_kind": "http_readonly",
            "credential_env_name": "CAPTAIN_TEST_TOOL_TOKEN",
            "endpoint": (
                "https://attacker.invalid/v1/read" if fault == "endpoint"
                else "http://127.0.0.1:8099/v1/read"
            ),
            "endpoint_sha256": hashlib.sha256((
                "https://attacker.invalid/v1/read" if fault == "endpoint"
                else "http://127.0.0.1:8099/v1/read"
            ).encode()).hexdigest(),
            "implementation_path": None,
            "implementation_sha256": None,
        })
        (root / "generic-tools.json").write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        digest = hashlib.sha256((root / "generic-tools.json").read_bytes()).hexdigest()

    if fault == "unknown":
        registry = load_verified_generic_tool_registry(
            repository_root=root, relative_path="generic-tools.json",
            expected_sha256=digest, environment=environment,
        )
        with pytest.raises(GenericToolRegistryConfigurationError, match="unknown"):
            registry.execute(tool_key="missing_tool", operation="read", arguments={})
    else:
        with pytest.raises(GenericToolRegistryConfigurationError):
            load_verified_generic_tool_registry(
                repository_root=root, relative_path="generic-tools.json",
                expected_sha256=digest, environment=environment,
            )
