from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

import agenten.agent_factory.hermetic_build_authority as hermetic_authority

from agenten.agent_factory.codex_build_execution import (
    CaptainStaticFactoryRunnerFactory,
    CaptainStaticFactorySourceBundle,
    FactoryCodexRuntimeAuthority,
)
from agenten.agent_factory.hermetic_build_authority import (
    CaptainFactoryV3PackageEvidenceIssuer,
    CaptainStaticFactoryBuildObserver,
    FactoryV3AssertionVerifier,
    minimal_hermetic_subprocess_environment,
)
from agenten.agent_factory.request_observer import (
    CaptainFactoryActionRequestCoordinator,
    FactoryCredentialRequiredObservation,
)
from agenten.agent_factory.orchestration import FactoryDispatchError
from agenten.agent_factory.worker_failure_projection import (
    CaptainFactoryWorkerFailureCoordinator,
)


def test_v3_authority_implementations_are_production_components() -> None:
    assert CaptainStaticFactoryRunnerFactory.__module__.startswith(
        "agenten.agent_factory"
    )
    assert CaptainStaticFactoryBuildObserver.__module__.startswith(
        "agenten.agent_factory"
    )
    assert CaptainFactoryV3PackageEvidenceIssuer.__module__.startswith(
        "agenten.agent_factory"
    )
    assert FactoryV3AssertionVerifier.__module__.startswith("agenten.agent_factory")
    assert CaptainFactoryActionRequestCoordinator.__module__.startswith(
        "agenten.agent_factory"
    )
    assert FactoryCredentialRequiredObservation.__module__.startswith(
        "agenten.agent_factory"
    )
    assert CaptainFactoryWorkerFailureCoordinator.__module__.startswith(
        "agenten.agent_factory"
    )
    assert not hasattr(hermetic_authority, "CaptainHermeticBuildExecutor")


def test_static_factory_runner_rejects_unapproved_source_digest(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    authority = FactoryCodexRuntimeAuthority(
        workspace_root=workspace,
        writable_roots=((workspace / "src").resolve(),),
        allowed_commands=(("python", "-m", "compileall", "src"),),
        network_allowed=False,
        maximum_provider_cost_usd=1.0,
    )
    factory = CaptainStaticFactoryRunnerFactory(
        authority=authority,
        bundle=CaptainStaticFactorySourceBundle(
            (("src/__init__.py", b'"""bounded"""\n'),)
        ),
        expected_bundle_sha256="0" * 64,
        interpreter_path=Path(sys.executable),
    )

    with pytest.raises(FactoryDispatchError, match="digest is not authorized"):
        factory(
            session_id="session",
            state_path=tmp_path / "state.json",
            journal_path=tmp_path / "journal.jsonl",
            maximum_runtime_seconds=10,
            deadline_at=datetime.now(timezone.utc),
        )


def test_hermetic_subprocess_environment_excludes_unrelated_host_secrets(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FACTORY_SENTINEL_SECRET", "must-not-cross")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "must-not-cross-either")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    environment = minimal_hermetic_subprocess_environment(
        credential_ref="credential://tenant-a/crm-readonly",
        host_environment=os.environ,
        temporary_directory=Path(os.environ.get("TEMP", ".")),
    )

    assert environment["FACTORY_CREDENTIAL_REF"] == (
        "credential://tenant-a/crm-readonly"
    )
    assert "PATH" in environment
    assert "FACTORY_SENTINEL_SECRET" not in environment
    assert "UNRELATED_API_TOKEN" not in environment
