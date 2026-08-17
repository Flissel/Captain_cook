from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gateway.capability_benchmark_tools import (
    GenericToolRegistryConfigurationError,
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
            "implementation_path": "readonly_adapter.py",
            "implementation_sha256": adapter_sha,
        }],
    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    subprocess.run(("git", "add", "."), cwd=root, check=True)
    subprocess.run(("git", "commit", "-q", "-m", "test: registry"), cwd=root, check=True)
    return root, hashlib.sha256(registry.read_bytes()).hexdigest()


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
