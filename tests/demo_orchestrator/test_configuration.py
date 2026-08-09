from __future__ import annotations

from pathlib import Path
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
    def binding(service: str, *, execute: bool = False) -> object:
        return SimpleNamespace(
            service=service,
            base_url=f"http://127.0.0.1/{service}",
            health_url=f"http://127.0.0.1/{service}/healthz",
            execute_url=(f"http://127.0.0.1/{service}/v1/execute" if execute else None),
            authentication="bearer",
        )

    values = {
        "schema_name": "captain.capability-production-bundle-manifest.v1",
        "provider_supervisor_mode": "dynamic_gateway_context_v1",
        "provider_context_reference_header": "X-Captain-Provider-Context-Ref",
        "manifest_sha256": "a" * 64,
        "gateway": binding("gateway"),
        "benchmark": binding("benchmark", execute=True),
        "runtime": binding("runtime"),
        "provider_proxy": binding("provider_proxy"),
        "minibook": binding("minibook"),
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


def test_missing_real_d11_loader_reports_exact_gap(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(
        DemoConfigurationError,
        match="D11 operator process bootstrap is unavailable",
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


def test_missing_d10_configuration_is_reported_only_at_paid_preflight(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    configuration = load_demo_configuration(
        repository,
        revision_reader=lambda _: "b" * 40,
        production_manifest_loader=lambda *, environment: _manifest(),
    )

    with pytest.raises(
        DemoConfigurationError,
        match="D10 private benchmark catalog capability is unavailable",
    ):
        configuration.require_private_benchmark_catalog()


def test_missing_minibook_readback_and_process_control_report_exact_d11_gaps(
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
    with pytest.raises(DemoConfigurationError, match="D11 operator process bootstrap is unavailable"):
        configuration.require_operator_process_bootstrap()


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
