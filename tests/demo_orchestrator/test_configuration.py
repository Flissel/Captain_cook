from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agenten.demo_orchestrator.configuration import (
    DemoConfigurationError,
    initialize_private_environment,
    load_demo_configuration,
)
from agenten.demo_orchestrator.contracts import CANONICAL_INPUT_PATHS


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    for index, relative in enumerate(CANONICAL_INPUT_PATHS, start=1):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# canonical input {index}\n", encoding="utf-8")
    initialize_private_environment(repository, reporter=lambda _: None)
    return repository


def _manifest(**updates: object) -> object:
    credential_names = {
        "gateway": "CAPTAIN_GATEWAY_TOKEN",
        "benchmark": "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",
        "tool_host": "CAPTAIN_CAPABILITY_TOOL_HOST_TOKEN",
        "runtime": "CAPTAIN_RUNTIME_TOKEN",
        "factory": "CAPTAIN_FACTORY_RUNTIME_TOKEN",
        "provider_proxy": "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "minibook": "MINIBOOK_API_KEY",
        "n8n": "CAPTAIN_N8N_API_KEY",
    }

    def binding(service: str, *, execute: bool = False) -> object:
        base = f"http://127.0.0.1/{service}"
        health_suffix = {
            "gateway": "/v1/capability-assemblies/health",
            "runtime": "/health",
            "factory": "/healthz",
            "minibook": "/health",
            "provider_proxy": "/v1/healthz",
            "benchmark": "/healthz",
            "tool_host": "/healthz",
            "n8n": "/api/v1/workflows?limit=1",
        }[service]
        prefix = ("python", "-m", "gateway.capability_process_operator", "--service", service)
        return SimpleNamespace(
            service=service,
            base_url=base,
            health_url=f"{base}{health_suffix}",
            execute_url=(f"{base}/v1/execute" if execute else None),
            authentication=("x-n8n-api-key" if service == "n8n" else "bearer"),
            credential_env=credential_names[service],
            authenticated_health_url=(
                f"{base}/api/v1/projection-health"
                if service == "minibook" else f"{base}{health_suffix}"
            ),
            authenticated_health_credential_env=(
                "MINIBOOK_PROJECTION_API_KEY"
                if service == "minibook" else credential_names[service]
            ),
            authenticated_health_header=("X-N8N-API-KEY" if service == "n8n" else "Authorization"),
            start_argv=(*prefix, "start"),
            stop_argv=(*prefix, "stop"),
            restart_argv=(*prefix, "restart"),
        )

    values = {
        "schema_name": "captain.capability-production-bundle-manifest.v1",
        "provider_supervisor_mode": "dynamic_gateway_context_v1",
        "provider_context_reference_header": "X-Captain-Provider-Context-Ref",
        "manifest_sha256": "a" * 64,
        "provider_policy_sha256": "1" * 64,
        "provider_price_card_sha256": "2" * 64,
        "provider_context_sha256": "3" * 64,
        "provider_proxy_executable_sha256": "4" * 64,
        "provider_proxy_code_sha256": "5" * 64,
        "provider_paid_command_sha256": "6" * 64,
        "provider_hard_ceiling_enforced": True,
        "gateway": binding("gateway"),
        "benchmark": binding("benchmark", execute=True),
        "tool_host": binding("tool_host"),
        "runtime": binding("runtime"),
        "factory": binding("factory", execute=True),
        "provider_proxy": binding("provider_proxy"),
        "minibook": binding("minibook"),
        "n8n": binding("n8n"),
    }
    values.update(updates)
    return SimpleNamespace(**values)


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
    assert "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN=" in text
    assert "MINIBOOK_API_KEY=" in text
    assert "MINIBOOK_PROJECTION_API_KEY=" in text
    assert "CAPTAIN_DEMO_BUNDLE" not in text
    assert all("=" not in message for message in messages)
    assert not list(private_env.parent.glob("*.tmp"))


def test_init_reuses_existing_root_authority_without_copy_or_overlap(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    root_env = repository / ".env"
    root_env.write_text("CAPTAIN_GATEWAY_TOKEN=operator-owned\n", encoding="utf-8")

    private_env = initialize_private_environment(repository, reporter=lambda _: None)

    assert root_env.read_text(encoding="utf-8") == "CAPTAIN_GATEWAY_TOKEN=operator-owned\n"
    assert "CAPTAIN_GATEWAY_TOKEN=" not in private_env.read_text(encoding="utf-8")


def test_real_d11_loader_reports_incomplete_configuration(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(
        DemoConfigurationError,
        match="D11 production bundle configuration is incomplete",
    ):
        load_demo_configuration(repository)


def test_load_binds_real_d11_manifest_and_repository_inputs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manifest = _manifest()

    configuration = load_demo_configuration(
        repository,
        revision_reader=lambda _: "b" * 40,
        production_manifest_loader=lambda *, environment: manifest,
    )

    assert configuration.production_manifest is manifest
    assert configuration.bundle_sha256 == "a" * 64
    assert configuration.repository_revision == "b" * 40
    assert set(configuration.canonical_input_digests) == set(CANONICAL_INPUT_PATHS)
    assert len(set(configuration.canonical_input_digests.values())) == 3
    assert configuration.provider_context_reference_header == "X-Captain-Provider-Context-Ref"
    provider = configuration.production_manifest.provider_proxy
    assert provider.base_url == "http://127.0.0.1/provider_proxy"
    assert provider.health_url == "http://127.0.0.1/provider_proxy/v1/healthz"
    assert provider.credential_env == "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN"


def test_real_d11_manifest_builder_is_compatible_with_d12_loader(tmp_path: Path) -> None:
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    candidates = [
        Path(line.removeprefix("worktree "))
        for line in listing
        if line.startswith("worktree ")
        and line.endswith("capability-production-bundle-d11")
    ]
    if not candidates:
        pytest.skip("D11 worktree is unavailable before integration")
    d11_root = candidates[0]
    environment = {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:18080",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-token",
        "CAPTAIN_CAPABILITY_BENCHMARK_URL": "http://127.0.0.1:18081/v1/execute",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN": "benchmark-token",
        "CAPTAIN_CAPABILITY_BENCHMARK_GATEWAY_TOKEN": "benchmark-gateway-token",
        "CAPTAIN_CAPABILITY_TOOL_HOST_URL": "http://127.0.0.1:18086",
        "CAPTAIN_CAPABILITY_TOOL_HOST_TOKEN": "tool-host-token",
        "CAPTAIN_RUNTIME_URL": "http://127.0.0.1:18082",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-token",
        "CAPTAIN_FACTORY_RUNTIME_URL": "http://127.0.0.1:18085",
        "CAPTAIN_FACTORY_RUNTIME_TOKEN": "factory-runtime-token",
        "CAPTAIN_FACTORY_RUNTIME_CODE_SHA256": "7" * 64,
        "CAPTAIN_PROVIDER_PROXY_URL": "http://127.0.0.1:18083",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "proxy-token",
        "CAPTAIN_PROVIDER_POLICY_SHA256": "1" * 64,
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256": "2" * 64,
        "CAPTAIN_PROVIDER_CONTEXT_SHA256": "3" * 64,
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256": "4" * 64,
        "CAPTAIN_PROVIDER_PROXY_CODE_SHA256": "5" * 64,
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256": "6" * 64,
        "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED": "true",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:18084",
        "MINIBOOK_API_KEY": "minibook-token",
        "MINIBOOK_PROJECTION_API_KEY": "projection-token",
    }
    script = (
        "import json,sys;"
        "from gateway.capability_assembly_bootstrap import "
        "load_capability_production_bundle_manifest as load;"
        "print(load(environment=json.loads(sys.stdin.read())).model_dump_json(by_alias=True))"
    )
    process_environment = os.environ.copy()
    process_environment["PYTHONPATH"] = str(d11_root)
    common_git = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    project_python = common_git.parent / ".venv/Scripts/python.exe"
    python = project_python if project_python.is_file() else Path(sys.executable)
    result = subprocess.run(
        [str(python), "-c", script],
        input=json.dumps(environment),
        capture_output=True,
        text=True,
        cwd=d11_root,
        env=process_environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    manifest = SimpleNamespace(
        schema_name=payload.pop("schema"),
        **{
            name: SimpleNamespace(**value) if isinstance(value, dict) else value
            for name, value in payload.items()
        },
    )
    repository = _repository(tmp_path)

    configuration = load_demo_configuration(
        repository,
        revision_reader=lambda _: "b" * 40,
        production_manifest_loader=lambda *, environment: manifest,
    )

    assert configuration.production_manifest.provider_proxy.health_url == (
        "http://127.0.0.1:18083/v1/healthz"
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"provider_supervisor_mode": "static"}, "D11 production bundle manifest is invalid"),
        ({"provider_context_reference_header": "X-Static"}, "D11 production bundle manifest is invalid"),
    ),
)
def test_load_rejects_non_dynamic_provider_contract(
    tmp_path: Path, updates: dict[str, object], message: str
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(DemoConfigurationError, match=message):
        load_demo_configuration(
            repository,
            revision_reader=lambda _: "b" * 40,
            production_manifest_loader=lambda *, environment: _manifest(**updates),
        )


@pytest.mark.parametrize(
    ("service", "field", "value"),
    (
        ("gateway", "health_url", "http://127.0.0.1/gateway/healthz"),
        ("runtime", "health_url", "http://127.0.0.1/runtime/healthz"),
        ("minibook", "health_url", "http://127.0.0.1/minibook/healthz"),
        ("provider_proxy", "health_url", "http://127.0.0.1/provider_proxy/healthz"),
        ("provider_proxy", "credential_env", "CAPTAIN_GATEWAY_TOKEN"),
    ),
)
def test_load_rejects_noncanonical_authenticated_health_binding(
    tmp_path: Path, service: str, field: str, value: str
) -> None:
    repository = _repository(tmp_path)
    manifest = _manifest()
    changed_binding = SimpleNamespace(
        **{
            **vars(getattr(manifest, service)),
            field: value,
        }
    )
    setattr(manifest, service, changed_binding)

    with pytest.raises(
        DemoConfigurationError,
        match="D11 production bundle manifest is invalid",
    ):
        load_demo_configuration(
            repository,
            revision_reader=lambda _: "b" * 40,
            production_manifest_loader=lambda *, environment: manifest,
        )


def test_missing_d10_configuration_is_reported_only_at_paid_preflight(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    configuration = load_demo_configuration(
        repository,
        revision_reader=lambda _: "b" * 40,
        production_manifest_loader=lambda *, environment: _manifest(),
    )

    with pytest.raises(
        DemoConfigurationError,
        match="D10 private benchmark catalog configuration is incomplete",
    ):
        configuration.require_private_benchmark_catalog()


def test_process_controls_are_typed_and_minibook_gap_remains_exact(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    configuration = load_demo_configuration(
        repository,
        revision_reader=lambda _: "b" * 40,
        production_manifest_loader=lambda *, environment: _manifest(),
    )

    with pytest.raises(DemoConfigurationError, match="D11 Minibook assembly evidence readback capability is unavailable"):
        configuration.require_minibook_evidence_readback()
    controls = configuration.require_operator_process_bootstrap()
    assert tuple(control.service for control in controls.services) == (
        "gateway", "runtime", "factory", "provider_proxy", "benchmark", "tool_host", "minibook", "n8n"
    )
    runtime = configuration.require_runtime_restart_control()
    assert runtime.service == "runtime"
    assert runtime.restart_argv[-1] == "restart"


def test_duplicate_canonical_input_bytes_fail_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository / CANONICAL_INPUT_PATHS[0]
    second = repository / CANONICAL_INPUT_PATHS[1]
    second.write_bytes(first.read_bytes())

    with pytest.raises(DemoConfigurationError, match="canonical demo inputs must have distinct bytes"):
        load_demo_configuration(
            repository,
            revision_reader=lambda _: "b" * 40,
            production_manifest_loader=lambda *, environment: _manifest(),
        )
