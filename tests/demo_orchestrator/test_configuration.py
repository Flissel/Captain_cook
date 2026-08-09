from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agenten.demo_orchestrator.configuration import (
    DemoConfigurationError,
    initialize_private_environment,
    load_demo_configuration,
)


CANONICAL_INPUTS = (
    "demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md",
    "demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bundle(repository: Path, private_env: Path) -> tuple[Path, str]:
    for index, relative in enumerate(CANONICAL_INPUTS, start=1):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# canonical input {index}\n", encoding="utf-8")
    catalog = repository / ".captain-cook/live-demo/private/catalog.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text('{"schema":"captain.private-demo-catalog.v1"}\n', encoding="utf-8")
    compose = repository / "docker-compose.test.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    module = repository / "gateway/app.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("# sealed gateway\n", encoding="utf-8")
    services = []
    for offset, name in enumerate(("gateway", "provider_proxy", "runtime", "benchmark")):
        environment = [
            {
                "gateway": "CAPTAIN_GATEWAY_TOKEN",
                "provider_proxy": "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
                "runtime": "CAPTAIN_RUNTIME_TOKEN",
                "benchmark": "CAPTAIN_BENCHMARK_TOKEN",
            }[name]
        ]
        if name == "provider_proxy":
            environment.append("OPENAI_API_KEY")
        services.append(
            {
                "name": name,
                "argv": ["{python}", "-m", f"captain_demo_{name}"],
                "working_directory": ".",
                "port": 18100 + offset,
                "health_url": (
                    "http://127.0.0.1:18100/v1/capability-assemblies/health"
                    if name == "gateway"
                    else f"http://127.0.0.1:{18100 + offset}/health"
                ),
                "health_token_env": environment[0],
                "environment": environment,
                "artifact_paths": ["gateway/app.py"],
            }
        )
    payload = {
        "schema": "captain.capability-demo-bundle.v1",
        "repository_revision": "a" * 40,
        "canonical_inputs": {relative: _sha(repository / relative) for relative in CANONICAL_INPUTS},
        "artifact_digests": {"gateway/app.py": _sha(module)},
        "compose_files": {"docker-compose.test.yml": _sha(compose)},
        "compose_project": "captain-capability-demo",
        "benchmark_catalog_path": ".captain-cook/live-demo/private/catalog.json",
        "benchmark_catalog_sha256": _sha(catalog),
        "gateway_base_url": "http://127.0.0.1:18100",
        "minibook_evidence_url_template": "http://127.0.0.1:18104/v1/projections/{assembly_id}",
        "minibook_token_env": "MINIBOOK_PROJECTION_API_KEY",
        "services": services,
    }
    bundle = repository / ".captain-cook/live-demo/private/bundle.json"
    bundle.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    digest = _sha(bundle)
    with private_env.open("a", encoding="utf-8") as stream:
        stream.write(f"CAPTAIN_DEMO_BUNDLE_MANIFEST={bundle}\n")
        stream.write(f"CAPTAIN_DEMO_BUNDLE_MANIFEST_SHA256={digest}\n")
    return bundle, digest


def test_init_is_atomic_redacted_idempotent_and_never_creates_provider_key(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    root_env = repository / ".env"
    root_env.write_text("OPENAI_API_KEY=operator-owned\n", encoding="utf-8")
    messages: list[str] = []

    private_env = initialize_private_environment(repository, reporter=messages.append)
    first_bytes = private_env.read_bytes()
    initialize_private_environment(repository, reporter=messages.append)

    assert private_env.read_bytes() == first_bytes
    assert root_env.read_text(encoding="utf-8") == "OPENAI_API_KEY=operator-owned\n"
    text = first_bytes.decode("utf-8")
    assert "OPENAI_API_KEY" not in text
    assert "CAPTAIN_GATEWAY_TOKEN=" in text
    assert "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN=" in text
    assert "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN=" in text
    assert "CAPTAIN_RUNTIME_TOKEN=" in text
    assert "CAPTAIN_BENCHMARK_TOKEN=" in text
    assert "MINIBOOK_PROJECTION_API_KEY=" in text
    assert all("=" not in message for message in messages)
    assert not list(private_env.parent.glob("*.tmp"))


def test_missing_d11_bundle_fails_with_exact_gap_before_external_effect(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    initialize_private_environment(repository, reporter=lambda _: None)

    with pytest.raises(
        DemoConfigurationError,
        match="D11 sealed capability demo bundle is unavailable",
    ):
        load_demo_configuration(repository)


def test_load_validates_sealed_bundle_canonical_inputs_and_provider_key_isolation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".env").write_text("OPENAI_API_KEY=operator-owned\n", encoding="utf-8")
    private_env = initialize_private_environment(repository, reporter=lambda _: None)
    _, digest = _write_bundle(repository, private_env)

    configuration = load_demo_configuration(
        repository, revision_reader=lambda _: "a" * 40
    )

    assert configuration.bundle_sha256 == digest
    assert set(configuration.bundle.canonical_inputs) == set(CANONICAL_INPUTS)
    assert tuple(service.name for service in configuration.bundle.services) == (
        "gateway",
        "provider_proxy",
        "runtime",
        "benchmark",
    )
    assert "OPENAI_API_KEY" in configuration.environment_for("provider_proxy")
    for service in ("gateway", "runtime", "benchmark"):
        assert "OPENAI_API_KEY" not in configuration.environment_for(service)


def test_changed_bundle_bytes_fail_before_bundle_is_used(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    private_env = initialize_private_environment(repository, reporter=lambda _: None)
    bundle, _ = _write_bundle(repository, private_env)
    bundle.write_text("{}\n", encoding="utf-8")

    with pytest.raises(DemoConfigurationError, match="bundle digest mismatch"):
        load_demo_configuration(repository, revision_reader=lambda _: "a" * 40)


def test_missing_d10_catalog_reports_exact_preflight_gap(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".env").write_text("OPENAI_API_KEY=operator-owned\n", encoding="utf-8")
    private_env = initialize_private_environment(repository, reporter=lambda _: None)
    _write_bundle(repository, private_env)
    (repository / ".captain-cook/live-demo/private/catalog.json").unlink()

    with pytest.raises(
        DemoConfigurationError,
        match="D10 private benchmark catalog is unavailable",
    ):
        load_demo_configuration(repository, revision_reader=lambda _: "a" * 40)


def test_bundle_rejects_three_paths_when_two_inputs_have_same_bytes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".env").write_text("OPENAI_API_KEY=operator-owned\n", encoding="utf-8")
    private_env = initialize_private_environment(repository, reporter=lambda _: None)
    bundle, _ = _write_bundle(repository, private_env)
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    paths = list(payload["canonical_inputs"])
    payload["canonical_inputs"][paths[1]] = payload["canonical_inputs"][paths[0]]
    bundle.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    private = private_env.read_text(encoding="utf-8")
    private = private.replace(
        next(line for line in private.splitlines() if line.startswith("CAPTAIN_DEMO_BUNDLE_MANIFEST_SHA256=")),
        f"CAPTAIN_DEMO_BUNDLE_MANIFEST_SHA256={_sha(bundle)}",
    )
    private_env.write_text(private, encoding="utf-8")

    with pytest.raises(DemoConfigurationError, match="bundle is invalid"):
        load_demo_configuration(repository, revision_reader=lambda _: "a" * 40)
