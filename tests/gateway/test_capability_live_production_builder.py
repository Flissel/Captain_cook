from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agenten.agent_runtime import production_bootstrap
from gateway.capability_live_production_builder import (
    CapabilityLiveProductionConfigurationError,
    build_current_authority_capability_live_runner_from_environment,
)


VALID_AUTHORITY_ADAPTER = """
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_live_production_builder import CapabilityLiveCurrentAuthorities
from gateway.capability_live_runtime_resume import GatewayCapabilityResumeExecutionAdapter

class Provisioning:
    def provision(self, request):
        raise AssertionError("provider effect")

    def binding_for(self, request):
        raise AssertionError("provider effect")

class ProjectionEffects:
    async def project_and_ack(self, event):
        raise AssertionError("provider effect")

    async def readback(self, event_id):
        raise AssertionError("provider effect")

def build_capability_live_authorities(context):
    return CapabilityLiveCurrentAuthorities(
        factory=object.__new__(AgentFactoryLiveComposition),
        provisioning=Provisioning(),
        minibook_effects=ProjectionEffects(),
        resume_execution=GatewayCapabilityResumeExecutionAdapter(gateway=object()),
    )
""".strip()


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "authority-loader@example.invalid")
    _git(repository, "config", "user.name", "Authority Loader Test")
    return repository


def _commit_adapter(repository: Path, source: str = VALID_AUTHORITY_ADAPTER) -> Path:
    adapter = repository / "capability_authorities.py"
    adapter.write_text(source, encoding="utf-8")
    _git(repository, "add", adapter.name)
    _git(repository, "commit", "--quiet", "-m", "test: add authority adapter")
    return adapter


def _environment(adapter: Path) -> dict[str, str]:
    return {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "test-token",
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5.6-terra",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:3456",
        "MINIBOOK_PROJECTION_API_KEY": "test-projection-key",
        "CAPTAIN_CAPABILITY_LIVE_ADAPTER": adapter.name,
        "CAPTAIN_CAPABILITY_LIVE_ADAPTER_SHA256": hashlib.sha256(
            adapter.read_bytes()
        ).hexdigest(),
    }


def test_authority_loader_rejects_untracked_adapter_before_module_effect(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "module-effect"
    adapter = repository / "capability_authorities.py"
    adapter.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="committed|owned",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


def test_authority_loader_rejects_dirty_adapter_before_module_effect(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "module-effect"
    adapter = _commit_adapter(repository)
    adapter.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="committed|dirty",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


def test_authority_loader_rejects_digest_mismatch_before_module_effect(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "module-effect"
    adapter = _commit_adapter(
        repository,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
    )
    environment = _environment(adapter)
    environment["CAPTAIN_CAPABILITY_LIVE_ADAPTER_SHA256"] = "0" * 64

    with pytest.raises(CapabilityLiveProductionConfigurationError, match="digest"):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=environment,
        )

    assert not marker.exists()


def test_authority_loader_rejects_final_handle_reparse_escape_before_read_or_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "module-effect"
    adapter = _commit_adapter(
        repository,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
    )
    monkeypatch.setattr(
        production_bootstrap,
        "_final_path_for_open_file",
        lambda stream, *, requested_path: tmp_path / "reparse-escape.py",
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="outside|final path",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


def test_authority_loader_rejects_handle_swap_without_executing_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository)
    marker = tmp_path / "replacement-effect"
    replacement = tmp_path / "replacement.py"
    replacement.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
        encoding="utf-8",
    )

    def swap_after_open(stream: object, *, requested_path: Path) -> Path:
        del stream
        replacement.replace(requested_path)
        return requested_path

    monkeypatch.setattr(
        production_bootstrap,
        "_final_path_for_open_file",
        swap_after_open,
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="changed|identity|final path",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


@pytest.mark.parametrize(
    "factory_source",
    (
        "build_capability_live_authorities = 42",
        "async def build_capability_live_authorities(context):\n    raise AssertionError('factory effect')",
        "def build_capability_live_authorities():\n    raise AssertionError('factory effect')",
    ),
    ids=("noncallable", "async", "wrong-arity"),
)
def test_authority_loader_rejects_invalid_factory_contract_before_factory_effect(
    tmp_path: Path,
    factory_source: str,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository, factory_source)

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="factory.*(callable|synchronous|signature|unavailable)",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )


@pytest.mark.parametrize(
    ("override", "expected"),
    (
        ("Provisioning.provision = 42", r"provisioning\.provision"),
        (
            "async def broken(self, request):\n    raise AssertionError('provider effect')\n"
            "Provisioning.provision = broken",
            r"provisioning\.provision",
        ),
        (
            "def broken(self):\n    raise AssertionError('provider effect')\n"
            "Provisioning.provision = broken",
            r"provisioning\.provision",
        ),
        ("ProjectionEffects.readback = 42", r"minibook_effects\.readback"),
    ),
    ids=("noncallable", "sync-required", "wrong-arity", "async-noncallable"),
)
def test_authority_loader_rejects_invalid_port_bindings_without_invoking_ports(
    tmp_path: Path,
    override: str,
    expected: str,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository, VALID_AUTHORITY_ADAPTER + "\n" + override)

    with pytest.raises(CapabilityLiveProductionConfigurationError, match=expected):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )


def test_authority_loader_accepts_committed_clean_digest_bound_adapter(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository)

    runner = build_current_authority_capability_live_runner_from_environment(
        repository_root=repository,
        environment=_environment(adapter),
    )

    assert runner._pipeline._execution._resume_execution is not None
