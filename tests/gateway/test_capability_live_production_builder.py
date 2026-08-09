from __future__ import annotations

import hashlib
import subprocess
import traceback
from pathlib import Path

import pytest

from agenten.agent_runtime import production_bootstrap
from gateway.agent_factory_live_composition import AgentFactoryLiveComposition
from gateway.capability_live_production_builder import (
    CapabilityLiveAuthorityAdapterContext,
    CapabilityLiveProductionConfigurationError,
    CapabilityLiveProductionSettings,
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

class Repository:
    def snapshot(self, job_id):
        raise AssertionError("repository effect")

    def workflow_artifacts(self, job_id):
        raise AssertionError("repository effect")

    def workflow_budget_projection(self, job_id):
        raise AssertionError("repository effect")

    def business_benchmark_summary_by_artifact(self, reference):
        raise AssertionError("repository effect")

class Coordinator:
    def projection(self, job_id):
        raise AssertionError("coordinator effect")

class Runner:
    async def run(self, job_id, **kwargs):
        raise AssertionError("runner effect")

class FactoryComposition(AgentFactoryLiveComposition):
    pass

def build_capability_live_authorities(context):
    return CapabilityLiveCurrentAuthorities(
        factory=FactoryComposition(
            repository=Repository(),
            coordinator=Coordinator(),
            lease_issuer=object(),
            hermes=object(),
            team_execution=object(),
            candidate_validator=object(),
            business_benchmark=object(),
            dispatcher=object(),
            runner=Runner(),
            improvement_authority=object(),
            runtime_retry_authority=None,
        ),
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


def _settings_environment() -> dict[str, str]:
    return {
        "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
        "CAPTAIN_GATEWAY_TOKEN": "gateway-secret-marker",
        "CAPTAIN_BENCHMARK_PROVIDER": "openai",
        "CAPTAIN_BENCHMARK_MODEL": "gpt-5.6-terra",
        "MINIBOOK_BACKEND_URL": "http://127.0.0.1:3456",
        "MINIBOOK_PROJECTION_API_KEY": "projection-secret-marker",
    }


def test_real_composition_exposes_the_complete_read_only_capability_contract() -> None:
    values = {
        "snapshot": object(),
        "workflow_artifacts": (object(),),
        "workflow_budget_projection": object(),
        "business_benchmark_summary_by_artifact": object(),
    }

    class Repository:
        def snapshot(self, job_id: object) -> object:
            assert job_id == "job-id"
            return values["snapshot"]

        def workflow_artifacts(self, job_id: object) -> tuple[object, ...]:
            assert job_id == "job-id"
            return values["workflow_artifacts"]

        def workflow_budget_projection(self, job_id: object) -> object:
            assert job_id == "job-id"
            return values["workflow_budget_projection"]

        def business_benchmark_summary_by_artifact(self, reference: object) -> object:
            assert reference == "artifact-ref"
            return values["business_benchmark_summary_by_artifact"]

    composition = AgentFactoryLiveComposition(
        repository=Repository(),  # type: ignore[arg-type]
        coordinator=object(),  # type: ignore[arg-type]
        lease_issuer=object(),  # type: ignore[arg-type]
        hermes=object(),  # type: ignore[arg-type]
        team_execution=object(),  # type: ignore[arg-type]
        candidate_validator=object(),  # type: ignore[arg-type]
        business_benchmark=object(),  # type: ignore[arg-type]
        dispatcher=object(),  # type: ignore[arg-type]
        runner=object(),  # type: ignore[arg-type]
        improvement_authority=object(),  # type: ignore[arg-type]
        runtime_retry_authority=None,
    )

    assert composition.snapshot("job-id") is values["snapshot"]
    assert composition.workflow_artifacts("job-id") == values["workflow_artifacts"]
    assert (
        composition.workflow_budget_projection("job-id")
        is values["workflow_budget_projection"]
    )
    assert (
        composition.business_benchmark_summary_by_artifact("artifact-ref")
        is values["business_benchmark_summary_by_artifact"]
    )


def test_authority_settings_and_context_repr_redact_secrets() -> None:
    settings = CapabilityLiveProductionSettings.from_environment(
        _settings_environment()
    )
    context = CapabilityLiveAuthorityAdapterContext(
        repository_root=Path("repository"),
        settings=settings,
    )

    rendered = repr(settings) + repr(context)

    assert "gateway-secret-marker" not in rendered
    assert "projection-secret-marker" not in rendered


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


def test_authority_loader_ignores_inherited_git_repository_overrides_before_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository)
    marker = tmp_path / "module-effect"
    redirected_source = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('effect')\n"
        f"{VALID_AUTHORITY_ADAPTER}\n"
    )

    alternate_repository = tmp_path / "alternate-repository"
    alternate_repository.mkdir()
    _git(alternate_repository, "init", "--quiet")
    _git(
        alternate_repository,
        "config",
        "user.email",
        "authority-loader@example.invalid",
    )
    _git(alternate_repository, "config", "user.name", "Authority Loader Test")
    alternate_adapter = alternate_repository / adapter.name
    alternate_adapter.write_text(redirected_source, encoding="utf-8")
    _git(alternate_repository, "add", alternate_adapter.name)
    _git(alternate_repository, "commit", "--quiet", "-m", "test: redirect git")

    adapter.write_text(redirected_source, encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(alternate_repository / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(alternate_repository))
    monkeypatch.setenv(
        "GIT_INDEX_FILE",
        str(alternate_repository / ".git" / "index"),
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="committed|dirty|repository",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


def test_authority_loader_rejects_custom_clean_filter_before_module_effect(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository)
    attributes = repository / ".gitattributes"
    attributes.write_text(
        f"{adapter.name} filter=cloak\n",
        encoding="utf-8",
    )
    _git(repository, "add", attributes.name)
    _git(repository, "commit", "--quiet", "-m", "test: add custom clean filter")
    _git(
        repository,
        "config",
        "filter.cloak.clean",
        f"git show HEAD:{adapter.name}",
    )
    _git(repository, "config", "filter.cloak.required", "true")
    marker = tmp_path / "filtered-module-effect"
    adapter.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="attribute|filter|committed|dirty|factory",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


@pytest.mark.parametrize(
    "attribute",
    ("working-tree-encoding=UTF-16", "ident"),
)
def test_authority_loader_rejects_unsafe_git_attributes(
    tmp_path: Path,
    attribute: str,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(repository)
    attributes = repository / ".gitattributes"
    attributes.write_text(f"{adapter.name} {attribute}\n", encoding="utf-8")
    _git(repository, "add", attributes.name)
    _git(repository, "commit", "--quiet", "-m", "test: add unsafe attribute")

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="attribute",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )


def test_authority_loader_rejects_dirty_git_attributes_before_module_effect(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "dirty-attributes-effect"
    adapter = _commit_adapter(
        repository,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n"
        + VALID_AUTHORITY_ADAPTER,
    )
    (repository / ".gitattributes").write_text(
        f"{adapter.name} text\n",
        encoding="utf-8",
    )

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="attributes.*dirty",
    ):
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=_environment(adapter),
        )

    assert not marker.exists()


@pytest.mark.parametrize("source", ("info", "config"))
def test_authority_loader_rejects_external_git_attributes_before_module_effect(
    tmp_path: Path,
    source: str,
) -> None:
    repository = _repository(tmp_path)
    marker = tmp_path / "external-attributes-effect"
    adapter = _commit_adapter(
        repository,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('effect')\n"
        + VALID_AUTHORITY_ADAPTER,
    )
    if source == "info":
        git_path = _git(repository, "rev-parse", "--git-path", "info/attributes")
        attributes = Path(git_path.stdout.decode().strip())
        if not attributes.is_absolute():
            attributes = repository / attributes
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_text(f"{adapter.name} ident\n", encoding="utf-8")
    else:
        attributes = tmp_path / "external-attributes"
        attributes.write_text(f"{adapter.name} text\n", encoding="utf-8")
        _git(repository, "config", "core.attributesfile", str(attributes))

    with pytest.raises(
        CapabilityLiveProductionConfigurationError,
        match="external Git attributes",
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


def test_authority_loader_suppresses_secret_bearing_factory_cause(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    adapter = _commit_adapter(
        repository,
        "def build_capability_live_authorities(context):\n"
        "    raise RuntimeError(repr(context))\n",
    )
    environment = {
        **_settings_environment(),
        "CAPTAIN_CAPABILITY_LIVE_ADAPTER": adapter.name,
        "CAPTAIN_CAPABILITY_LIVE_ADAPTER_SHA256": hashlib.sha256(
            adapter.read_bytes()
        ).hexdigest(),
    }

    with pytest.raises(CapabilityLiveProductionConfigurationError) as failure:
        build_current_authority_capability_live_runner_from_environment(
            repository_root=repository,
            environment=environment,
        )

    rendered = "".join(
        traceback.format_exception(
            type(failure.value),
            failure.value,
            failure.value.__traceback__,
        )
    )
    assert "gateway-secret-marker" not in rendered
    assert "projection-secret-marker" not in rendered
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True


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


@pytest.mark.parametrize(
    ("override", "expected"),
    (
        (
            "FactoryComposition.snapshot = 42",
            r"factory\.snapshot",
        ),
        (
            "async def broken(self, job_id):\n    raise AssertionError('effect')\n"
            "FactoryComposition.workflow_artifacts = broken",
            r"factory\.workflow_artifacts",
        ),
        (
            "def broken(self):\n    raise AssertionError('effect')\n"
            "FactoryComposition.workflow_budget_projection = broken",
            r"factory\.workflow_budget_projection",
        ),
    ),
    ids=("noncallable", "synchronous-required", "wrong-arity"),
)
def test_authority_loader_rejects_incomplete_composition_contract_without_effects(
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
